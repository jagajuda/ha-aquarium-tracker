"""Configuration model for Aquarium Tracker (pure Python, no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MODES = ("cycling", "maintenance")
USER_EVENT_TYPES = ("dose", "measurement", "water_change", "fertilize", "maintenance", "note")
SYSTEM_EVENT_TYPES = ("entity", "system")
SCHEDULE_TYPES = ("interval", "daily", "weekly", "monthly")

# Mass fraction of NH4+ in the dosed compound.
COMPOUNDS = {
    "ammonium_chloride": 18.039 / 53.491,  # NH4Cl
    "ammonium": 1.0,  # solution already expressed as NH4+
}


class ConfigError(Exception):
    """Raised when tanks.yaml is invalid."""


@dataclass
class Parameter:
    key: str
    unit: str = ""


@dataclass
class Schedule:
    type: str
    at: time
    days: int = 1
    weekdays: list[int] = field(default_factory=list)
    weekday: int = 6
    nth: int = 1
    months: int = 0  # interval only: step in calendar months instead of days
    start: date | None = None  # interval only: first due date when nothing is logged yet


@dataclass
class Reminder:
    repeat_count: int = 3
    repeat_interval_min: int = 60


@dataclass
class Task:
    key: str
    title: str
    event: str
    schedule: Schedule
    reminder: Reminder
    modes: list[str]
    early_window_h: float = 6.0
    # None, "cycle" (decided by cycle phase) or explicit parameter list
    params: list[str] | str | None = None
    # maintenance tasks: actions done in turn (a single `action` becomes a 1-item list)
    rotate: list[str] | None = None
    # fertilize tasks: only events containing one of these products complete the task
    products: list[str] | None = None


@dataclass
class StockSolution:
    key: str
    name: str
    nh4_mg_per_ml: float


@dataclass
class Fertilizer:
    key: str
    name: str
    default_dose: float | None
    unit: str
    prefill: bool = True  # prefill default_dose in a manual (not task-opened) form


@dataclass
class MaintenanceAction:
    key: str
    name: str


@dataclass
class WatchEntity:
    entity_id: str
    label: str
    name: str
    states: list[str] | None = None


@dataclass
class CycleConfig:
    params: list[str] = field(default_factory=lambda: ["NH4", "NO2"])
    required_passes: int = 2
    test_window_h: float = 24.0
    zero_threshold: float = 0.0
    verify_every_days: int = 1


@dataclass
class Band:
    factor: float
    gt: float | None = None   # applies when value > gt
    gte: float | None = None  # applies when value >= gte (neither: catch-all)

    def matches(self, v: float) -> bool:
        if self.gt is not None:
            return v > self.gt
        if self.gte is not None:
            return v >= self.gte
        return True


@dataclass
class DosingProgram:
    """Measure-driven dosing programme (e.g. nitrogen titration)."""

    key: str
    title: str
    product: str
    parameter: str
    ml_per_drop: float
    test_dose_ml: float
    response_h: float
    response_tolerance_h: float
    label_per_ml_per_l: float  # test-visible rise per mL in 1 L (label-derived k, scaled by tank volume)
    k_tolerance: float  # measured k accepted within this fraction of the label k
    kit_resolution: float  # smaller rises are not trusted -> label k
    target: float
    ramp_max_ml_per_day: float
    maintenance_start_ml_per_day: float
    bands: list[Band]
    stable_low: float
    stable_high: float
    stable_count: int
    dose_schedule: Schedule
    dose_early_h: float
    measure_schedule: Schedule
    measure_early_h: float
    reminder: Reminder


@dataclass
class Tank:
    key: str
    name: str
    net_volume_l: float
    initial_mode: str
    parameters: list[str]
    stock_solutions: dict[str, StockSolution]
    fertilizers: dict[str, Fertilizer]
    maintenance_actions: dict[str, MaintenanceAction]
    water_change_default_percent: float | None
    tasks: dict[str, Task]
    watch_entities: list[WatchEntity]
    cycle: CycleConfig
    default_temp_c: float | None
    programs: dict[str, DosingProgram] = field(default_factory=dict)


@dataclass
class TrackerConfig:
    parameters: dict[str, Parameter]
    tanks: dict[str, Tank]
    snooze_min: int = 60


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _parse_time(value: Any, where: str) -> time:
    # YAML 1.1 parses unquoted 21:00 as sexagesimal integer 1260 (minutes)
    if isinstance(value, int) and not isinstance(value, bool):
        return time((value // 60) % 24, value % 60)
    if isinstance(value, str):
        try:
            hh, mm = value.strip().split(":")
            return time(int(hh), int(mm))
        except (ValueError, TypeError):
            pass
    raise ConfigError(f"{where}: invalid time {value!r}, expected 'HH:MM'")


def _weekday(value: Any, where: str) -> int:
    key = str(value).strip().lower()[:3]
    _require(key in WEEKDAYS, f"{where}: invalid weekday {value!r}")
    return WEEKDAYS[key]


def _key(value: Any, where: str) -> str:
    _require(
        isinstance(value, str) and value.replace("_", "").isalnum() and value.isascii(),
        f"{where}: key {value!r} must be ASCII letters, digits and underscores",
    )
    return value


def _parse_schedule(raw: Any, where: str) -> Schedule:
    _require(isinstance(raw, dict), f"{where}: schedule must be a mapping")
    stype = raw.get("type")
    _require(stype in SCHEDULE_TYPES, f"{where}: schedule.type must be one of {SCHEDULE_TYPES}")
    sched = Schedule(type=stype, at=_parse_time(raw.get("at", "09:00"), f"{where}.at"))
    if stype == "interval":
        if "months" in raw:
            _require("days" not in raw, f"{where}: use either days or months, not both")
            sched.months = int(raw["months"])
            _require(sched.months >= 1, f"{where}: months must be >= 1")
        else:
            sched.days = int(raw.get("days", 1))
            _require(sched.days >= 1, f"{where}: days must be >= 1")
        if raw.get("start") is not None:
            st = raw["start"]
            if isinstance(st, date):
                sched.start = st
            else:
                try:
                    sched.start = date.fromisoformat(str(st).strip())
                except ValueError as err:
                    raise ConfigError(f"{where}.start: invalid date {st!r}, expected 'YYYY-MM-DD'") from err
    elif "start" in raw or "months" in raw:
        raise ConfigError(f"{where}: start/months are only valid for interval schedules")
    elif stype == "weekly":
        wds = raw.get("weekdays")
        _require(isinstance(wds, list) and wds, f"{where}: weekly schedule needs weekdays list")
        sched.weekdays = sorted({_weekday(w, where) for w in wds})
    elif stype == "monthly":
        sched.weekday = _weekday(raw.get("weekday", "sun"), where)
        sched.nth = int(raw.get("nth", 1))
        _require(sched.nth in (1, 2, 3, 4, -1), f"{where}: nth must be 1..4 or -1 (last)")
    return sched


def parse_config(raw: Any) -> TrackerConfig:
    """Validate the raw YAML dict and build the model."""
    _require(isinstance(raw, dict), "tanks.yaml: top level must be a mapping")
    settings = raw.get("settings") or {}
    snooze_min = int(settings.get("snooze_min", 60))
    default_temp = settings.get("default_temp_c")

    params_raw = raw.get("parameters") or {}
    _require(isinstance(params_raw, dict) and params_raw, "parameters: at least one parameter required")
    parameters: dict[str, Parameter] = {}
    for pkey, pval in params_raw.items():
        pval = pval or {}
        parameters[str(pkey)] = Parameter(key=str(pkey), unit=str(pval.get("unit", "")))

    tanks_raw = raw.get("tanks") or {}
    _require(isinstance(tanks_raw, dict) and tanks_raw, "tanks: at least one tank required")
    tanks: dict[str, Tank] = {}
    for tkey, traw in tanks_raw.items():
        where = f"tanks.{tkey}"
        _key(tkey, where)
        _require(isinstance(traw, dict), f"{where}: must be a mapping")
        volume = traw.get("net_volume_l")
        _require(isinstance(volume, (int, float)) and volume > 0, f"{where}: net_volume_l required (> 0)")
        mode = traw.get("initial_mode", "maintenance")
        _require(mode in MODES, f"{where}: initial_mode must be one of {MODES}")

        tank_params = traw.get("parameters") or list(parameters)
        for p in tank_params:
            _require(p in parameters, f"{where}.parameters: unknown parameter {p!r}")

        stock: dict[str, StockSolution] = {}
        for skey, sraw in (traw.get("stock_solutions") or {}).items():
            swhere = f"{where}.stock_solutions.{skey}"
            _key(skey, swhere)
            if "nh4_mg_per_ml" in sraw:
                mg_ml = float(sraw["nh4_mg_per_ml"])
            else:
                compound = sraw.get("compound", "ammonium_chloride")
                _require(compound in COMPOUNDS, f"{swhere}: unknown compound {compound!r}")
                _require("g_per_100ml" in sraw, f"{swhere}: g_per_100ml or nh4_mg_per_ml required")
                # g/100 mL -> mg/mL is x10
                mg_ml = float(sraw["g_per_100ml"]) * 10.0 * COMPOUNDS[compound]
            stock[skey] = StockSolution(key=skey, name=str(sraw.get("name", skey)), nh4_mg_per_ml=mg_ml)

        ferts: dict[str, Fertilizer] = {}
        for fkey, fraw in (traw.get("fertilizers") or {}).items():
            _key(fkey, f"{where}.fertilizers.{fkey}")
            fraw = fraw or {}
            dd = fraw.get("default_dose")
            ferts[fkey] = Fertilizer(
                key=fkey,
                name=str(fraw.get("name", fkey)),
                default_dose=float(dd) if dd is not None else None,
                unit=str(fraw.get("unit", "mL")),
                prefill=bool(fraw.get("prefill", True)),
            )

        actions: dict[str, MaintenanceAction] = {}
        for akey, araw in (traw.get("maintenance_actions") or {}).items():
            awhere = f"{where}.maintenance_actions.{akey}"
            _key(akey, awhere)
            _require(
                akey not in USER_EVENT_TYPES + SYSTEM_EVENT_TYPES,
                f"{awhere}: key {akey!r} clashes with a built-in event type",
            )
            actions[akey] = MaintenanceAction(key=akey, name=str((araw or {}).get("name", akey)))

        craw = traw.get("cycle") or {}
        cycle = CycleConfig(
            params=list(craw.get("params", ["NH4", "NO2"])),
            required_passes=int(craw.get("required_passes", 2)),
            test_window_h=float(craw.get("test_window_h", 24)),
            zero_threshold=float(craw.get("zero_threshold", 0)),
            verify_every_days=int(craw.get("verify_every_days", 1)),
        )
        for p in cycle.params:
            _require(p in parameters, f"{where}.cycle.params: unknown parameter {p!r}")
        _require("NH4" in cycle.params, f"{where}.cycle.params must include NH4")

        tasks: dict[str, Task] = {}
        for k, raw_task in (traw.get("tasks") or {}).items():
            twhere = f"{where}.tasks.{k}"
            _key(k, twhere)
            _require(isinstance(raw_task, dict), f"{twhere}: must be a mapping")
            event = raw_task.get("event")
            _require(event in USER_EVENT_TYPES, f"{twhere}: event must be one of {USER_EVENT_TYPES}")
            params = raw_task.get("params")
            if params is not None and params != "cycle":
                _require(event == "measurement", f"{twhere}: params only valid for measurement tasks")
                if params == "all":
                    params = list(tank_params)
                _require(isinstance(params, list), f"{twhere}: params must be a list, 'all' or 'cycle'")
                for p in params:
                    _require(p in parameters, f"{twhere}.params: unknown parameter {p!r}")
            rotate = raw_task.get("rotate")
            if raw_task.get("action") is not None:
                _require(rotate is None, f"{twhere}: use either action or rotate, not both")
                rotate = [raw_task["action"]]
            if event == "maintenance":
                _require(
                    isinstance(rotate, list) and rotate,
                    f"{twhere}: maintenance tasks need action or rotate",
                )
                for a in rotate:
                    _require(a in actions, f"{twhere}: unknown maintenance action {a!r}")
            else:
                _require(rotate is None, f"{twhere}: action/rotate only valid for maintenance tasks")
            products = raw_task.get("products")
            if products is not None:
                _require(event == "fertilize", f"{twhere}: products only valid for fertilize tasks")
                _require(isinstance(products, list) and products, f"{twhere}: products must be a non-empty list")
                for fp in products:
                    _require(fp in ferts, f"{twhere}: unknown fertilizer {fp!r}")
            default_modes = ["cycling"] if params == "cycle" else ["maintenance"]
            modes = list(raw_task.get("modes", default_modes))
            for m in modes:
                _require(m in MODES, f"{twhere}.modes: invalid mode {m!r}")
            rraw = raw_task.get("reminder") or {}
            tasks[k] = Task(
                key=k,
                title=str(raw_task.get("title", k)),
                event=event,
                schedule=_parse_schedule(raw_task.get("schedule"), f"{twhere}.schedule"),
                reminder=Reminder(
                    repeat_count=int(rraw.get("repeat_count", 3)),
                    repeat_interval_min=int(rraw.get("repeat_interval_min", 60)),
                ),
                modes=modes,
                early_window_h=float(raw_task.get("early_window_h", 6)),
                params=params,
                rotate=list(rotate) if rotate else None,
                products=list(products) if products else None,
            )

        watch: list[WatchEntity] = []
        for i, wraw in enumerate(traw.get("watch_entities") or []):
            wwhere = f"{where}.watch_entities[{i}]"
            _require(isinstance(wraw, dict) and "entity_id" in wraw, f"{wwhere}: entity_id required")
            label = _key(wraw.get("label", "entity"), wwhere)
            _require(
                label not in USER_EVENT_TYPES + SYSTEM_EVENT_TYPES and label not in actions,
                f"{wwhere}: label {label!r} clashes with a built-in event type or maintenance action",
            )
            states = wraw.get("states")
            watch.append(
                WatchEntity(
                    entity_id=str(wraw["entity_id"]),
                    label=label,
                    name=str(wraw.get("name", label)),
                    states=[str(s) for s in states] if states else None,
                )
            )

        programs: dict[str, DosingProgram] = {}
        for pkey, praw in (traw.get("dosing_programs") or {}).items():
            pwhere = f"{where}.dosing_programs.{pkey}"
            _key(pkey, pwhere)
            _require(pkey not in tasks, f"{pwhere}: key clashes with a task")
            _require(isinstance(praw, dict), f"{pwhere}: must be a mapping")
            product = praw.get("product")
            _require(product in ferts, f"{pwhere}: product must be one of the tank's fertilizers")
            param = praw.get("parameter", "NO3")
            _require(param in parameters, f"{pwhere}: unknown parameter {param!r}")
            bands = []
            for i, b in enumerate(praw.get("bands") or []):
                _require(isinstance(b, dict) and "factor" in b, f"{pwhere}.bands[{i}]: factor required")
                bands.append(Band(factor=float(b["factor"]),
                                  gt=float(b["gt"]) if "gt" in b else None,
                                  gte=float(b["gte"]) if "gte" in b else None))
            _require(bool(bands) and bands[-1].gt is None and bands[-1].gte is None,
                     f"{pwhere}.bands: last band must be a catch-all (factor only)")
            stable = praw.get("stable_band", [10, 15])
            rraw = praw.get("reminder") or {}
            programs[pkey] = DosingProgram(
                key=pkey,
                title=str(praw.get("title", pkey)),
                product=product,
                parameter=param,
                ml_per_drop=float(praw.get("ml_per_drop", 0.05)),
                test_dose_ml=float(praw.get("test_dose_ml", 1)),
                response_h=float(praw.get("response_h", 24)),
                response_tolerance_h=float(praw.get("response_tolerance_h", 4)),
                label_per_ml_per_l=float(praw["label_per_ml_per_l"]),
                k_tolerance=float(praw.get("k_tolerance", 0.5)),
                kit_resolution=float(praw.get("kit_resolution", 1)),
                target=float(praw["target"]),
                ramp_max_ml_per_day=float(praw.get("ramp_max_ml_per_day", 0.5)),
                maintenance_start_ml_per_day=float(praw["maintenance_start_ml_per_day"]),
                bands=bands,
                stable_low=float(stable[0]),
                stable_high=float(stable[1]),
                stable_count=int(praw.get("stable_count", 3)),
                dose_schedule=Schedule(type="daily", at=_parse_time(praw.get("dose_at", "18:00"), f"{pwhere}.dose_at")),
                dose_early_h=float(praw.get("dose_early_h", 6)),
                measure_schedule=_parse_schedule(
                    {"type": "weekly", **(praw.get("measure") or {"weekdays": ["sat"], "at": "13:00"})},
                    f"{pwhere}.measure"),
                measure_early_h=float(praw.get("measure_early_h", 24)),
                reminder=Reminder(int(rraw.get("repeat_count", 3)), int(rraw.get("repeat_interval_min", 60))),
            )
            _require(programs[pkey].ml_per_drop > 0, f"{pwhere}: ml_per_drop must be > 0")

        wc_default = traw.get("water_change_default_percent")
        tanks[tkey] = Tank(
            key=tkey,
            name=str(traw.get("name", tkey)),
            net_volume_l=float(volume),
            initial_mode=mode,
            parameters=list(tank_params),
            stock_solutions=stock,
            fertilizers=ferts,
            maintenance_actions=actions,
            water_change_default_percent=float(wc_default) if wc_default is not None else None,
            tasks=tasks,
            watch_entities=watch,
            cycle=cycle,
            programs=programs,
            default_temp_c=float(traw.get("default_temp_c", default_temp)) if (traw.get("default_temp_c", default_temp) is not None) else None,
        )

    return TrackerConfig(parameters=parameters, tanks=tanks, snooze_min=snooze_min)
