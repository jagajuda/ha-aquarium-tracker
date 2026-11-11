"""Runtime core: event log, task scheduling, notifications."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import NOTIFY_ACTION_PREFIX, PANEL_URL_PATH, SIGNAL_UPDATED, TICK_SECONDS
from .logic import CycleStatus, compute_due, dose_nh4_mg_l, evaluate_cycle, free_nh3_mg_l
from .model import MODES, USER_EVENT_TYPES, Tank, Task, TrackerConfig
from .store import EventStore

_LOGGER = logging.getLogger(__name__)

# Turkish UI names for built-in labels
LABEL_NAMES = {
    "dose": "Dozlama",
    "measurement": "Ölçüm",
    "water_change": "Su değişimi",
    "fertilize": "Gübreleme",
    "note": "Not",
    "system": "Sistem",
}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _fmt_local(ts: datetime) -> str:
    return dt_util.as_local(ts).strftime("%d.%m %H:%M")


class AquariumTracker:
    """Owns config, event cache, task state and notifications."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: TrackerConfig,
        store: EventStore,
        notify_service: str,
    ) -> None:
        self.hass = hass
        self.config = config
        self.store = store
        self.notify_service = notify_service
        self._events: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        await self._reload_events()
        self._state = await self.hass.async_add_executor_job(self.store.get_state)
        now = dt_util.utcnow()
        for tank in self.config.tanks.values():
            if f"mode:{tank.key}" not in self._state:
                await self._set_state(f"mode:{tank.key}", tank.initial_mode)
            for task in tank.tasks.values():
                st = self._task_state(tank.key, task.key)
                if "anchor" not in st:
                    st["anchor"] = now.isoformat()
                    await self._save_task_state(tank.key, task.key, st)

        self._unsubs.append(
            async_track_time_interval(self.hass, self._tick, timedelta(seconds=TICK_SECONDS))
        )
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._on_notification_action)
        )
        watch_map: dict[str, list[tuple[Tank, Any]]] = {}
        for tank in self.config.tanks.values():
            for w in tank.watch_entities:
                watch_map.setdefault(w.entity_id, []).append((tank, w))
        if watch_map:
            self._watch_map = watch_map
            self._unsubs.append(
                async_track_state_change_event(self.hass, list(watch_map), self._on_entity_change)
            )
        await self.async_evaluate()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ------------------------------------------------------------ state helpers

    async def _set_state(self, key: str, value: Any) -> None:
        self._state[key] = value
        await self.hass.async_add_executor_job(self.store.set_state, key, value)

    def _task_state(self, tank: str, task: str) -> dict[str, Any]:
        return dict(self._state.get(f"task:{tank}:{task}", {}))

    async def _save_task_state(self, tank: str, task: str, st: dict[str, Any]) -> None:
        await self._set_state(f"task:{tank}:{task}", st)

    async def _reload_events(self) -> None:
        self._events = await self.hass.async_add_executor_job(self.store.list_events)

    def _changed(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    # ------------------------------------------------------------ queries

    def mode(self, tank_key: str) -> str:
        return self._state.get(f"mode:{tank_key}", self.config.tanks[tank_key].initial_mode)

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events

    def _logic_events(self, tank_key: str) -> list[dict[str, Any]]:
        return [
            {"id": e["id"], "type": e["type"], "ts": _parse(e["ts"]), "payload": e["payload"]}
            for e in self._events
            if e["tank"] == tank_key
        ]

    def cycle_status(self, tank: Tank, now: datetime | None = None) -> CycleStatus:
        return evaluate_cycle(self._logic_events(tank.key), tank.cycle, now or dt_util.utcnow())

    def has_cycle(self, tank: Tank) -> bool:
        return tank.initial_mode == "cycling" or any(t.params == "cycle" for t in tank.tasks.values())

    def latest_values(self, tank_key: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for e in reversed(self._events):
            if e["tank"] != tank_key or e["type"] != "measurement":
                continue
            for p, v in (e["payload"].get("values") or {}).items():
                if p not in out:
                    out[p] = {"value": v, "ts": e["ts"], "event_id": e["id"]}
                    derived = e["payload"].get("derived") or {}
                    if p == "NH4" and "NH3_free" in derived:
                        out[p]["NH3_free"] = derived["NH3_free"]
        return out

    def task_params(self, tank: Tank, task: Task, cs: CycleStatus | None = None) -> list[str] | None:
        if task.params == "cycle":
            return (cs or self.cycle_status(tank)).params
        return task.params if isinstance(task.params, list) else None

    def task_info(self, tank: Tank, task: Task, now: datetime | None = None) -> dict[str, Any]:
        now = now or dt_util.utcnow()
        st = self._task_state(tank.key, task.key)
        cs = self.cycle_status(tank, now) if task.params == "cycle" else None
        active = self.mode(tank.key) in task.modes and not (cs and cs.phase == "complete")
        info: dict[str, Any] = {
            "tank": tank.key,
            "task": task.key,
            "title": task.title,
            "event": task.event,
            "params": self.task_params(tank, task, cs),
            "active": active,
            "due": None,
            "status": "inactive",
            "snoozed_until": st.get("snoozed_until"),
            "cycle_phase": cs.phase if cs else None,
        }
        if not active:
            return info
        anchor = _parse(st["anchor"]) if "anchor" in st else now
        times = [_parse(e["ts"]) for e in self._events if e["tank"] == tank.key and e["type"] == task.event]
        interval_days = tank.cycle.verify_every_days if (cs and cs.phase == "verify") else None
        due = compute_due(
            task.schedule,
            now,
            dt_util.get_default_time_zone(),
            times,
            anchor,
            task.early_window_h,
            interval_days,
        )
        info["due"] = due.astimezone(dt_util.UTC).isoformat()
        snoozed = st.get("snoozed_until")
        if now < due:
            info["status"] = "upcoming"
        elif snoozed and now < _parse(snoozed) and st.get("due") == info["due"]:
            info["status"] = "snoozed"
        else:
            info["status"] = "due"
        return info

    def all_tasks(self) -> list[dict[str, Any]]:
        now = dt_util.utcnow()
        return [
            self.task_info(tank, task, now)
            for tank in self.config.tanks.values()
            for task in tank.tasks.values()
        ]

    def labels(self) -> list[dict[str, str]]:
        out = [{"key": k, "name": LABEL_NAMES[k]} for k in (*USER_EVENT_TYPES, "system")]
        seen = {o["key"] for o in out}
        for tank in self.config.tanks.values():
            for w in tank.watch_entities:
                if w.label not in seen:
                    out.append({"key": w.label, "name": w.name})
                    seen.add(w.label)
        return out

    def snapshot(self) -> dict[str, Any]:
        """Everything the panel needs except the event list."""
        now = dt_util.utcnow()
        tanks = []
        for tank in self.config.tanks.values():
            cs = self.cycle_status(tank, now) if self.has_cycle(tank) else None
            tanks.append(
                {
                    "key": tank.key,
                    "name": tank.name,
                    "net_volume_l": tank.net_volume_l,
                    "mode": self.mode(tank.key),
                    "parameters": tank.parameters,
                    "stock_solutions": [
                        {"key": s.key, "name": s.name, "nh4_mg_per_ml": s.nh4_mg_per_ml}
                        for s in tank.stock_solutions.values()
                    ],
                    "fertilizers": [
                        {"key": f.key, "name": f.name, "default_dose": f.default_dose, "unit": f.unit}
                        for f in tank.fertilizers.values()
                    ],
                    "water_change_default_percent": tank.water_change_default_percent,
                    "default_temp_c": tank.default_temp_c,
                    "cycle": cs.as_dict() if cs else None,
                    "latest": self.latest_values(tank.key),
                    "tasks": [self.task_info(tank, t, now) for t in tank.tasks.values()],
                }
            )
        return {
            "parameters": {k: {"unit": p.unit} for k, p in self.config.parameters.items()},
            "labels": self.labels(),
            "snooze_min": self.config.snooze_min,
            "modes": list(MODES),
            "tanks": tanks,
        }

    # ------------------------------------------------------------ payloads

    def _normalize(self, tank: Tank, type_: str, payload: dict[str, Any], note: str) -> dict[str, Any]:
        payload = payload or {}
        if type_ == "dose":
            if not tank.stock_solutions:
                raise ValueError("Bu tank için stock solution tanımlı değil")
            key = payload.get("solution") or next(iter(tank.stock_solutions))
            if key not in tank.stock_solutions:
                raise ValueError(f"Bilinmeyen solüsyon: {key}")
            vol = float(payload.get("volume_ml") or 0)
            if vol <= 0:
                raise ValueError("Doz hacmi (mL) 0'dan büyük olmalı")
            sol = tank.stock_solutions[key]
            return {
                "solution": key,
                "solution_name": sol.name,
                "volume_ml": vol,
                "nh4_mg_l": round(dose_nh4_mg_l(vol, sol.nh4_mg_per_ml, tank.net_volume_l), 3),
            }
        if type_ == "measurement":
            raw = payload.get("values") or {}
            vals: dict[str, float] = {}
            for p, v in raw.items():
                if v is None or v == "":
                    continue
                if p not in self.config.parameters:
                    raise ValueError(f"Bilinmeyen parametre: {p}")
                vals[p] = float(v)
            if not vals:
                raise ValueError("En az bir ölçüm değeri gir")
            out: dict[str, Any] = {"values": vals}
            temp = vals.get("temp", tank.default_temp_c)
            if "NH4" in vals and "pH" in vals and temp is not None:
                out["derived"] = {
                    "NH3_free": round(free_nh3_mg_l(vals["NH4"], vals["pH"], temp), 4),
                    "temp_used": temp,
                }
            return out
        if type_ == "water_change":
            out = {}
            pct = payload.get("percent")
            if pct not in (None, ""):
                pct = float(pct)
                if not 0 < pct <= 100:
                    raise ValueError("Yüzde 0-100 arasında olmalı")
                out["percent"] = pct
                out["volume_l"] = round(pct / 100 * tank.net_volume_l, 1)
            vol = payload.get("volume_l")
            if vol not in (None, "") and "percent" not in out:
                out["volume_l"] = float(vol)
                out["percent"] = round(float(vol) / tank.net_volume_l * 100, 1)
            return out
        if type_ == "fertilize":
            doses: dict[str, float] = {}
            for k, v in (payload.get("doses") or {}).items():
                if v in (None, ""):
                    continue
                if k not in tank.fertilizers:
                    raise ValueError(f"Bilinmeyen gübre: {k}")
                doses[k] = float(v)
            if not doses:
                raise ValueError("En az bir gübre dozu gir")
            return {"doses": doses, "units": {k: tank.fertilizers[k].unit for k in doses}}
        if type_ == "note":
            if not note.strip():
                raise ValueError("Not boş olamaz")
            return {}
        return payload  # entity / system

    # ------------------------------------------------------------ mutations

    async def async_add_event(
        self,
        tank_key: str,
        type_: str,
        payload: dict[str, Any] | None = None,
        ts: datetime | None = None,
        note: str = "",
        source: str = "manual",
        label: str | None = None,
    ) -> dict[str, Any]:
        if tank_key not in self.config.tanks:
            raise ValueError(f"Bilinmeyen tank: {tank_key}")
        tank = self.config.tanks[tank_key]
        if source == "manual" and type_ not in USER_EVENT_TYPES:
            raise ValueError(f"Geçersiz event tipi: {type_}")
        norm = self._normalize(tank, type_, payload or {}, note or "")
        ev = await self.hass.async_add_executor_job(
            self.store.add_event,
            tank_key,
            ts or dt_util.utcnow(),
            type_,
            label or type_,
            norm,
            note or "",
            source,
        )
        await self._reload_events()
        await self.async_evaluate()
        self._changed()
        return ev

    async def async_update_event(
        self,
        event_id: int,
        ts: datetime | None = None,
        payload: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        old = await self.hass.async_add_executor_job(self.store.get_event, event_id)
        if old is None:
            raise ValueError("Event bulunamadı")
        tank = self.config.tanks.get(old["tank"])
        if payload is not None and tank is not None and old["type"] in USER_EVENT_TYPES:
            payload = self._normalize(tank, old["type"], payload, note if note is not None else old["note"])
        ev = await self.hass.async_add_executor_job(self.store.update_event, event_id, ts, payload, note)
        await self._reload_events()
        await self.async_evaluate()
        self._changed()
        return ev

    async def async_delete_event(self, event_id: int) -> None:
        try:
            await self.hass.async_add_executor_job(self.store.delete_event, event_id)
        except KeyError as err:
            raise ValueError("Event bulunamadı") from err
        await self._reload_events()
        await self.async_evaluate()
        self._changed()

    async def async_audit(self, event_id: int) -> list[dict[str, Any]]:
        return await self.hass.async_add_executor_job(self.store.audit_for, event_id)

    async def async_set_mode(self, tank_key: str, mode: str) -> None:
        if tank_key not in self.config.tanks or mode not in MODES:
            raise ValueError("Geçersiz tank veya mod")
        old = self.mode(tank_key)
        if old == mode:
            return
        await self._set_state(f"mode:{tank_key}", mode)
        now = dt_util.utcnow()
        tank = self.config.tanks[tank_key]
        for task in tank.tasks.values():
            if mode in task.modes:
                st = self._task_state(tank_key, task.key)
                st.update(anchor=now.isoformat(), due=None, sent=0, last_sent=None, snoozed_until=None)
                await self._save_task_state(tank_key, task.key, st)
        await self.hass.async_add_executor_job(
            self.store.add_event, tank_key, now, "system", "system",
            {"kind": "mode", "from": old, "to": mode}, "", "system",
        )
        await self._reload_events()
        await self.async_evaluate()
        self._changed()

    async def async_snooze(self, tank_key: str, task_key: str, minutes: int | None = None) -> None:
        if tank_key not in self.config.tanks or task_key not in self.config.tanks[tank_key].tasks:
            raise ValueError("Geçersiz görev")
        st = self._task_state(tank_key, task_key)
        until = dt_util.utcnow() + timedelta(minutes=minutes or self.config.snooze_min)
        st["snoozed_until"] = until.isoformat()
        await self._save_task_state(tank_key, task_key, st)
        self._changed()

    # ------------------------------------------------------------ evaluation

    @callback
    def _tick(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_evaluate())

    async def async_evaluate(self) -> None:
        async with self._lock:
            now = dt_util.utcnow()
            for tank in self.config.tanks.values():
                if self.has_cycle(tank) and self.mode(tank.key) == "cycling":
                    await self._check_cycle_complete(tank, now)
                for task in tank.tasks.values():
                    try:
                        await self._evaluate_task(tank, task, now)
                    except Exception:  # noqa: BLE001 - keep the loop alive
                        _LOGGER.exception("Task evaluation failed for %s/%s", tank.key, task.key)

    async def _check_cycle_complete(self, tank: Tank, now: datetime) -> None:
        cs = self.cycle_status(tank, now)
        key = f"cycle_notified:{tank.key}"
        notified = self._state.get(key, False)
        if cs.phase == "complete" and not notified:
            await self._set_state(key, True)
            await self.hass.async_add_executor_job(
                self.store.add_event, tank.key, now, "system", "system",
                {"kind": "cycle_complete", "passes": cs.passes}, "", "system",
            )
            await self._reload_events()
            self._changed()
            await self._notify(
                f"{tank.name}: cycle tamamlandı",
                f"{cs.passes} ardışık doz 24 saat içinde NH4=0 ve NO2=0. "
                "Panelden bakım moduna geçişi onayla.",
                f"aqt_{tank.key}_cycle",
                f"/{PANEL_URL_PATH}?tank={tank.key}",
                snooze_action=None,
            )
        elif cs.phase != "complete" and notified:
            await self._set_state(key, False)

    async def _evaluate_task(self, tank: Tank, task: Task, now: datetime) -> None:
        info = self.task_info(tank, task, now)
        st = self._task_state(tank.key, task.key)
        tag = f"aqt_{tank.key}_{task.key}"
        if not info["active"]:
            if st.get("sent"):
                await self._clear(tag)
                st.update(sent=0, last_sent=None, due=None)
                await self._save_task_state(tank.key, task.key, st)
            return
        if st.get("due") != info["due"]:
            if st.get("sent"):
                await self._clear(tag)  # occurrence completed or rolled over
            st.update(due=info["due"], sent=0, last_sent=None, snoozed_until=None)
            await self._save_task_state(tank.key, task.key, st)
            self._changed()
        due = _parse(info["due"])
        if now < due:
            return
        snoozed = _parse(st["snoozed_until"]) if st.get("snoozed_until") else None
        if snoozed and now < snoozed:
            return
        last = _parse(st["last_sent"]) if st.get("last_sent") else None
        sent = int(st.get("sent") or 0)
        send = False
        if sent == 0:
            send = True
        elif snoozed and last and last < snoozed:
            send = True
        elif sent <= task.reminder.repeat_count and last and now - last >= timedelta(
            minutes=task.reminder.repeat_interval_min
        ):
            send = True
        if not send:
            return
        title, message = self._task_message(tank, task, info, due, now)
        await self._notify(title, message, tag, f"/{PANEL_URL_PATH}?tank={tank.key}&task={task.key}",
                           snooze_action=f"{NOTIFY_ACTION_PREFIX}{tank.key}|{task.key}")
        st.update(sent=sent + 1, last_sent=now.isoformat())
        await self._save_task_state(tank.key, task.key, st)

    def _task_message(self, tank: Tank, task: Task, info: dict[str, Any], due: datetime, now: datetime) -> tuple[str, str]:
        title = f"{tank.name}: {task.title}"
        parts: list[str] = []
        if task.event == "measurement" and info["params"]:
            parts.append("Ölç: " + ", ".join(info["params"]))
            if info.get("cycle_phase") == "verify":
                parts.append(f"(son dozdan sonraki {tank.cycle.test_window_h:g} saat içinde)")
        elif task.event == "fertilize" and tank.fertilizers:
            parts.append(
                "Doz: " + ", ".join(
                    f"{f.name} {f.default_dose:g} {f.unit}" if f.default_dose is not None else f.name
                    for f in tank.fertilizers.values()
                )
            )
        elif task.event == "water_change":
            pct = tank.water_change_default_percent
            parts.append(f"Su değişimi zamanı{f' (%{pct:g})' if pct else ''}")
        if now - due > timedelta(minutes=30):
            parts.append(f"— gecikti, planlanan {_fmt_local(due)}")
        return title, " ".join(parts) or task.title

    # ------------------------------------------------------------ notifications

    async def _notify(self, title: str, message: str, tag: str, url: str, snooze_action: str | None) -> None:
        actions = []
        if snooze_action:
            actions.append({"action": snooze_action, "title": f"Ertele ({self.config.snooze_min} dk)"})
        actions.append({"action": "URI", "title": "Kaydet", "uri": url})
        data = {
            "tag": tag,
            "group": "aquarium",
            "url": url,
            "clickAction": url,
            "actions": actions,
        }
        await self._call_notify({"title": title, "message": message, "data": data})

    async def _clear(self, tag: str) -> None:
        await self._call_notify({"message": "clear_notification", "data": {"tag": tag}})

    async def _call_notify(self, service_data: dict[str, Any]) -> None:
        domain, _, service = self.notify_service.partition(".")
        if not self.hass.services.has_service(domain, service):
            _LOGGER.warning("Notify service %s not found", self.notify_service)
            return
        try:
            await self.hass.services.async_call(domain, service, service_data, blocking=False)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Notification failed")

    @callback
    def _on_notification_action(self, event: Event) -> None:
        action = event.data.get("action", "")
        if not action.startswith(NOTIFY_ACTION_PREFIX):
            return
        try:
            tank_key, task_key = action[len(NOTIFY_ACTION_PREFIX):].split("|", 1)
        except ValueError:
            return
        self.hass.async_create_task(self.async_snooze(tank_key, task_key))

    @callback
    def _on_entity_change(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or (old is not None and old.state == new.state):
            return
        for tank, w in self._watch_map.get(new.entity_id, []):
            if w.states is not None:
                if new.state not in w.states:
                    continue
            elif new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            self.hass.async_create_task(
                self.async_add_event(
                    tank.key,
                    "entity",
                    {"entity_id": new.entity_id, "name": w.name,
                     "from": old.state if old else None, "to": new.state},
                    ts=new.last_changed,
                    source="auto",
                    label=w.label,
                )
            )
