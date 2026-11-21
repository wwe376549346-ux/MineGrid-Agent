"""Metadata-driven multi-mine topology, voltage limit, and equivalent-circuit schema (Pydantic v2).

Onboarding a new coal mine distribution network only requires supplying a new MineProfile JSON/YAML
document matching this schema, without editing core health, diagnostic, or AC power-flow code.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent / "mine_profile.json"


class StationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=32)
    name: str
    role: Literal["surface_substation", "underground_substation", "distribution_point"]
    has_hourly_archive: bool = False
    diagram_filename: str | None = None


class VoltageLimitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nominal_kv: float = Field(..., gt=0)
    lo_kv: float = Field(..., gt=0)
    hi_kv: float = Field(..., gt=0)
    operational_watch_hi_kv: float = Field(..., gt=0)
    standard_ref: str

    @model_validator(mode="after")
    def _check_bounds(self) -> "VoltageLimitSpec":
        if self.lo_kv >= self.hi_kv:
            raise ValueError(f"lo_kv ({self.lo_kv}) must be less than hi_kv ({self.hi_kv})")
        return self


class BusSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    station: str
    name: str
    nominal_kv: float = Field(..., gt=0)
    segment: str
    reference_cabinet: str | None = None


class TransformerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    station: str
    hv_cabinet: str
    lv_cabinet: str
    hv_bus: str
    lv_bus: str
    default_sn_mva: float = Field(..., gt=0)
    default_vn_hv_kv: float = Field(..., gt=0)
    default_vn_lv_kv: float = Field(..., gt=0)
    default_vk_percent: float = Field(..., gt=0, lt=30)
    default_vkr_percent: float = Field(..., ge=0, lt=10)
    default_pfe_kw: float = Field(default=6.0, ge=0)
    default_i0_percent: float = Field(default=0.1, ge=0)
    tap_side: Literal["hv", "lv"] = "hv"
    tap_step_percent: float = Field(default=2.5, gt=0, le=10.0)
    tap_min: int = -2
    tap_max: int = 2
    calibrated_tap_pos: int = 0
    parameter_source: str = "parameterized_assumption"


class FeederSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cabinet: str
    station: str
    label: str
    bus_id: str
    role: Literal["incomer", "transformer_hv", "transformer_lv", "feeder", "capacitor", "spare", "tie"]
    nominal_kv: float = Field(..., gt=0)
    downstream_bus_id: str | None = None
    default_cable_length_km: float | None = Field(default=None, gt=0)
    default_r_ohm_per_km: float | None = Field(default=None, ge=0)
    default_x_ohm_per_km: float | None = Field(default=None, ge=0)
    default_max_i_ka: float | None = Field(default=None, gt=0)
    polarity_suspect: bool = False


class InterStationLinkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_station: str
    from_cabinet: str
    to_station: str
    to_cabinet: str
    description: str
    default_cable_length_km: float = Field(..., gt=0)
    default_r_ohm_per_km: float = Field(..., ge=0)
    default_x_ohm_per_km: float = Field(..., ge=0)
    default_max_i_ka: float = Field(..., gt=0)


class BusTieSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    station: str
    cabinet: str
    bus_a: str
    bus_b: str
    normal_closed: bool = False
    description: str


class BalanceEquationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    lhs_cabinets: list[str] = Field(..., min_length=1)
    rhs_cabinets: list[str] = Field(..., min_length=1)
    threshold_kw: float = Field(default=200.0, gt=0)


class MineProfile(BaseModel):
    """Declarative multi-mine topology & electrical configuration contract."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    mine_id: str = Field(..., min_length=1, max_length=64)
    mine_name: str
    primary_station: str
    polling_ramp_kw: float = Field(default=1000.0, gt=0)
    balance_threshold_kw: float = Field(default=200.0, gt=0)
    model_assumption_disclaimer: str
    stations: list[StationSpec] = Field(..., min_length=1)
    voltage_limits: list[VoltageLimitSpec] = Field(..., min_length=1)
    buses: list[BusSpec] = Field(..., min_length=1)
    transformers: list[TransformerSpec] = Field(..., min_length=1)
    feeders: list[FeederSpec] = Field(..., min_length=1)
    inter_station_links: list[InterStationLinkSpec] = Field(default_factory=list)
    bus_ties: list[BusTieSpec] = Field(default_factory=list)
    balance_equations: list[BalanceEquationSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_references(self) -> "MineProfile":
        station_codes = {s.code for s in self.stations}
        if self.primary_station not in station_codes:
            raise ValueError(f"primary_station {self.primary_station!r} not in stations {station_codes}")
        bus_ids = {b.id for b in self.buses}
        for b in self.buses:
            if b.station not in station_codes:
                raise ValueError(f"Bus {b.id} references unknown station {b.station!r}")
        for tr in self.transformers:
            if tr.hv_bus not in bus_ids or tr.lv_bus not in bus_ids:
                raise ValueError(f"Transformer {tr.id} references unknown bus ({tr.hv_bus} -> {tr.lv_bus})")
        for fd in self.feeders:
            if fd.bus_id not in bus_ids:
                raise ValueError(f"Feeder {fd.cabinet} references unknown bus {fd.bus_id!r}")
            if fd.downstream_bus_id and fd.downstream_bus_id not in bus_ids:
                raise ValueError(f"Feeder {fd.cabinet} references unknown downstream bus {fd.downstream_bus_id!r}")
        for tie in self.bus_ties:
            if tie.bus_a not in bus_ids or tie.bus_b not in bus_ids:
                raise ValueError(f"Bus tie {tie.id} references unknown bus ({tie.bus_a}, {tie.bus_b})")
        return self

    def spare_cabinets(self) -> set[str]:
        return {f.cabinet for f in self.feeders if f.role == "spare"}

    def capacitor_cabinets(self) -> set[str]:
        return {f.cabinet for f in self.feeders if f.role == "capacitor"}

    def incomer_cabinets(self) -> tuple[str, ...]:
        return tuple(f.cabinet for f in self.feeders if f.role == "incomer")

    def main_chain_cabinets(self) -> tuple[str, ...]:
        roles = ("incomer", "transformer_hv", "transformer_lv")
        ordered: list[str] = []
        for role in roles:
            ordered.extend(f.cabinet for f in self.feeders if f.role == role)
        return tuple(ordered)

    def feeder_map(self) -> dict[str, FeederSpec]:
        return {f.cabinet: f for f in self.feeders}

    def bus_map(self) -> dict[str, BusSpec]:
        return {b.id: b for b in self.buses}

    def voltage_limit_for_kv(self, nominal_kv: float) -> VoltageLimitSpec:
        for lim in self.voltage_limits:
            if abs(lim.nominal_kv - nominal_kv) < 1.0:
                return lim
        return min(self.voltage_limits, key=lambda x: abs(x.nominal_kv - nominal_kv))

    def derive_balance_equations(self) -> list[BalanceEquationSpec]:
        """Automatically derive KCL / power-conservation equations directly from graph topology."""
        equations: list[BalanceEquationSpec] = []
        primary_feeders = [f for f in self.feeders if f.station == self.primary_station]
        hv_incomers = [f.cabinet for f in primary_feeders if f.role == "incomer"]
        tr_hvs = [t.hv_cabinet for t in self.transformers if t.station == self.primary_station]
        tr_lvs = [t.lv_cabinet for t in self.transformers if t.station == self.primary_station]
        lv_feeders = [
            f.cabinet
            for f in primary_feeders
            if f.role in ("feeder", "capacitor") and not f.polarity_suspect
        ]

        if hv_incomers and tr_hvs:
            hv_kv = max((f.nominal_kv for f in primary_feeders if f.role == "incomer"), default=35.0)
            equations.append(
                BalanceEquationSpec(
                    id="AUTO_BAL_HV_IN_VS_TR",
                    name=f"{int(round(hv_kv))} kV 进线与主变高压侧功率守恒（图模自动生成）",
                    lhs_cabinets=hv_incomers,
                    rhs_cabinets=tr_hvs,
                    threshold_kw=self.balance_threshold_kw,
                )
            )
        for tr in self.transformers:
            if tr.station == self.primary_station:
                equations.append(
                    BalanceEquationSpec(
                        id=f"AUTO_BAL_{tr.id}_HV_LV",
                        name=f"{tr.name} 高低压侧功率守恒（图模自动生成）",
                        lhs_cabinets=[tr.hv_cabinet],
                        rhs_cabinets=[tr.lv_cabinet],
                        threshold_kw=self.balance_threshold_kw,
                    )
                )
        if tr_lvs and lv_feeders:
            lv_kv = min((f.nominal_kv for f in primary_feeders if f.role == "transformer_lv"), default=10.0)
            equations.append(
                BalanceEquationSpec(
                    id="AUTO_BAL_LV_IN_VS_FEEDERS",
                    name=f"{int(round(lv_kv))} kV 主变低压侧与实测出线功率守恒（图模自动生成）",
                    lhs_cabinets=tr_lvs,
                    rhs_cabinets=lv_feeders,
                    threshold_kw=self.balance_threshold_kw,
                )
            )
        return equations

    def effective_balance_equations(self) -> list[BalanceEquationSpec]:
        """Return declared balance equations or auto-derive them from graph topology if omitted."""
        if self.balance_equations:
            return list(self.balance_equations)
        return self.derive_balance_equations()

    def build_sld_layout(self, archive=None) -> dict:
        """Automatically compute hierarchical Single-Line Diagram layout from MineProfile topology."""
        st_map = {s.code: s for s in self.stations}
        primary_st = st_map[self.primary_station]
        primary_buses = [b for b in self.buses if b.station == self.primary_station]
        primary_feeders = [f for f in self.feeders if f.station == self.primary_station]
        fmap = {f.cabinet: f for f in primary_feeders}
        bmap = self.bus_map()

        hv_kv = max((b.nominal_kv for b in primary_buses), default=35.0)
        hv_buses = [b for b in primary_buses if abs(b.nominal_kv - hv_kv) < 1.0]
        lv_buses = [b for b in primary_buses if abs(b.nominal_kv - hv_kv) >= 1.0]
        hv_bus_ids = {b.id for b in hv_buses}
        lv_bus_ids = {b.id for b in lv_buses}

        # Group transformers and HV bays per HV bus
        primary_bays: list[dict] = []
        for hb in hv_buses:
            bay_incomers = [
                {"cabinet": f.cabinet, "label": f.label, "role": f.role, "nominal_kv": f.nominal_kv}
                for f in primary_feeders
                if f.bus_id == hb.id and f.role == "incomer"
            ]
            bay_spares = [
                {"cabinet": f.cabinet, "label": f.label, "role": f.role, "nominal_kv": f.nominal_kv}
                for f in primary_feeders
                if f.bus_id == hb.id and f.role == "spare"
            ]
            bay_trs = [
                {
                    "id": tr.id,
                    "name": tr.name,
                    "hv_cabinet": tr.hv_cabinet,
                    "hv_label": fmap[tr.hv_cabinet].label if tr.hv_cabinet in fmap else tr.hv_cabinet,
                    "lv_cabinet": tr.lv_cabinet,
                    "lv_label": fmap[tr.lv_cabinet].label if tr.lv_cabinet in fmap else tr.lv_cabinet,
                    "hv_bus": tr.hv_bus,
                    "lv_bus": tr.lv_bus,
                    "default_sn_mva": tr.default_sn_mva,
                    "default_vn_hv_kv": tr.default_vn_hv_kv,
                    "default_vn_lv_kv": tr.default_vn_lv_kv,
                }
                for tr in self.transformers
                if tr.hv_bus == hb.id
            ]
            primary_bays.append({
                "hv_bus": hb.model_dump(),
                "incomers": bay_incomers,
                "spares": bay_spares,
                "transformers": bay_trs,
            })

        # Separate HV vs LV bus ties on primary station
        hv_ties = [t.model_dump() for t in self.bus_ties if t.station == self.primary_station and t.bus_a in hv_bus_ids and t.bus_b in hv_bus_ids]
        lv_ties = [t.model_dump() for t in self.bus_ties if t.station == self.primary_station and t.bus_a in lv_bus_ids and t.bus_b in lv_bus_ids]

        # Build LV bus sections with outgoing feeders
        lv_sections: list[dict] = []
        for lb in lv_buses:
            lv_incomer_f = next((f for f in primary_feeders if f.bus_id == lb.id and f.role == "transformer_lv"), None)
            out_feeders = [
                {
                    "cabinet": f.cabinet,
                    "label": f.label,
                    "role": f.role,
                    "nominal_kv": f.nominal_kv,
                    "downstream_bus_id": f.downstream_bus_id,
                    "polarity_suspect": f.polarity_suspect,
                }
                for f in primary_feeders
                if f.bus_id == lb.id and f.role in ("feeder", "capacitor", "spare")
            ]
            lv_sections.append({
                "bus": lb.model_dump(),
                "lv_incomer": lv_incomer_f.cabinet if lv_incomer_f else lb.reference_cabinet,
                "feeders": out_feeders,
            })

        # Inter-station cable corridors
        corridors = [
            {
                "from_station": ln.from_station,
                "from_cabinet": ln.from_cabinet,
                "from_label": fmap[ln.from_cabinet].label if ln.from_cabinet in fmap else ln.from_cabinet,
                "to_station": ln.to_station,
                "to_cabinet": ln.to_cabinet,
                "description": ln.description,
                "default_cable_length_km": ln.default_cable_length_km,
            }
            for ln in self.inter_station_links
        ]

        # Downstream / underground stations auto-layout (reading cabinets from archive if not in feeders)
        downstream_stations: list[dict] = []
        downhole_cabinets: set[str] = {
            f.cabinet for f in primary_feeders if f.downstream_bus_id is not None
        } | {ln.from_cabinet for ln in self.inter_station_links}

        for st in self.stations:
            if st.code == self.primary_station:
                continue
            st_buses = [b for b in self.buses if b.station == st.code and any(ln.to_cabinet == b.reference_cabinet for ln in self.inter_station_links)]
            if not st_buses:
                st_buses = [b for b in self.buses if b.station == st.code]
            st_ties = [t for t in self.bus_ties if t.station == st.code]
            tie_cabs = {t.cabinet for t in st_ties}
            downhole_cabinets |= tie_cabs

            # Collect cabinets belonging to this downstream station
            st_cabs: list[tuple[str, str, str | None]] = [
                (f.cabinet, f.label, f.bus_id) for f in self.feeders if f.station == st.code
            ]
            if not st_cabs and archive is not None:
                rows = archive.db.execute(
                    "SELECT number, label FROM cabinet WHERE station = ? ORDER BY CAST(number AS INTEGER), number",
                    (st.code,),
                ).fetchall()
                st_cabs = [(r["number"], r["label"], None) for r in rows]

            sections_out: list[dict] = []
            # Map each bus to its incoming link & matching cabinets
            for idx, sb in enumerate(st_buses):
                in_link = next((ln for ln in self.inter_station_links if ln.to_station == st.code and ln.to_cabinet == sb.reference_cabinet), None)
                ref = sb.reference_cabinet or ""
                prefix = ref[:2] if len(ref) >= 3 else ""
                bus_cabs = []
                for num, lbl, explicit_bus in st_cabs:
                    if num in tie_cabs:
                        continue
                    if explicit_bus == sb.id or (explicit_bus is None and prefix and num.startswith(prefix)):
                        bus_cabs.append({"cabinet": num, "label": lbl, "is_incomer": num == ref})
                        downhole_cabinets.add(num)
                # Fallback if prefix didn't partition (e.g. non-numeric cabinet IDs on a new mine)
                if not bus_cabs and st_cabs:
                    non_tie = [(n, l) for n, l, _ in st_cabs if n not in tie_cabs]
                    chunk = max(1, (len(non_tie) + len(st_buses) - 1) // max(1, len(st_buses)))
                    for num, lbl in non_tie[idx * chunk : (idx + 1) * chunk]:
                        bus_cabs.append({"cabinet": num, "label": lbl, "is_incomer": num == ref})
                        downhole_cabinets.add(num)
                sections_out.append({
                    "bus": sb.model_dump(),
                    "incomer_cabinet": ref,
                    "upstream_cabinet": in_link.from_cabinet if in_link else None,
                    "cabinets": bus_cabs,
                })

            downstream_stations.append({
                "station": st.model_dump(),
                "total_cabinets": len(st_cabs),
                "ties": [t.model_dump() for t in st_ties],
                "sections": sections_out,
            })

        return {
            "mine_id": self.mine_id,
            "mine_name": self.mine_name,
            "primary_station": primary_st.model_dump(),
            "hv_nominal_kv": hv_kv,
            "lv_nominal_kv": min((b.nominal_kv for b in lv_buses), default=10.0),
            "primary_bays": primary_bays,
            "hv_ties": hv_ties,
            "lv_ties": lv_ties,
            "lv_sections": lv_sections,
            "inter_station_corridors": corridors,
            "downstream_stations": downstream_stations,
            "capacitor_cabinets": sorted(self.capacitor_cabinets()),
            "spare_cabinets": sorted(self.spare_cabinets()),
            "downhole_cabinets": sorted(downhole_cabinets),
            "voltage_limits": [v.model_dump() for v in self.voltage_limits],
            "auto_balance_equations": [eq.model_dump() for eq in self.derive_balance_equations()],
        }

    def validate_against_sqlite(self, archive) -> dict:
        """Cross-check declared primary station cabinets against the active SQLite archive."""
        db_cabs = {
            row["number"]
            for row in archive.db.execute(
                "SELECT number FROM cabinet WHERE station = ?",
                (self.primary_station,),
            )
        }
        profile_cabs = {f.cabinet for f in self.feeders if f.station == self.primary_station}
        missing_in_profile = sorted(db_cabs - profile_cabs, key=lambda x: (len(x), x))
        extra_in_profile = sorted(profile_cabs - db_cabs, key=lambda x: (len(x), x))
        return {
            "mine_id": self.mine_id,
            "primary_station": self.primary_station,
            "db_cabinet_count": len(db_cabs),
            "profile_cabinet_count": len(profile_cabs),
            "consistent": not missing_in_profile and not extra_in_profile,
            "missing_in_profile": missing_in_profile,
            "extra_in_profile": extra_in_profile,
        }

    def to_summary_dict(self) -> dict:
        return {
            **self.model_dump(),
            "derived": {
                "main_chain": list(self.main_chain_cabinets()),
                "incomers": list(self.incomer_cabinets()),
                "spare_cabinets": sorted(self.spare_cabinets(), key=lambda x: int(x) if x.isdigit() else 9999),
                "capacitor_cabinets": sorted(self.capacitor_cabinets(), key=lambda x: int(x) if x.isdigit() else 9999),
                "bus_count": len(self.buses),
                "transformer_count": len(self.transformers),
                "feeder_count": len(self.feeders),
                "inter_station_link_count": len(self.inter_station_links),
                "derived_balance_equations": [eq.model_dump() for eq in self.derive_balance_equations()],
            },
        }


@lru_cache(maxsize=8)
def _load_cached(resolved_path: str) -> MineProfile:
    raw = json.loads(Path(resolved_path).read_text(encoding="utf-8"))
    return MineProfile.model_validate(raw)


def load_mine_profile(path: str | Path | None = None) -> MineProfile:
    """Load and validate a MineProfile JSON file (defaults to laowadi/mine_profile.json)."""
    target = Path(path).resolve() if path else DEFAULT_PROFILE_PATH.resolve()
    return _load_cached(str(target))
