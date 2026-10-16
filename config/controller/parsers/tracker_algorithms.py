from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal

ReidPolicy = Literal[
    "never", "always", "with_reid", "unless_embedding_off", "with_any_reid"
]


@dataclass(frozen=True)
class BoxMotAlgorithm:
    class_path: str
    defaults: dict[str, Any]
    reid_policy: ReidPolicy = "never"
    excluded_common: frozenset[str] = frozenset()

    def needs_reid(self, params: dict[str, Any]) -> bool:
        if self.reid_policy == "never":
            return False
        if self.reid_policy == "always":
            return True
        if self.reid_policy == "with_reid":
            return bool(params.get("with_reid"))
        if self.reid_policy == "unless_embedding_off":
            return not bool(params.get("embedding_off"))
        return bool(params.get("with_reid") or params.get("with_longterm_reid"))


COMMON_DEFAULTS: dict[str, Any] = {
    "det_thresh": 0.3,
    "max_age": 30,
    "max_obs": 50,
    "min_hits": 3,
    "iou_threshold": 0.3,
    "per_class": False,
    "asso_func": "iou",
}


def needs_frame(algorithm: str, params: dict[str, Any]) -> bool:
    """Pixel requirements include camera-motion compensation as well as ReID."""
    spec = BOXMOT_ALGORITHMS[algorithm]
    if spec.needs_reid(params):
        return True
    if algorithm in ("bytetrack", "ocsort", "sfsort"):
        return False
    if algorithm == "deepocsort":
        return not params.get("cmc_off", False)
    if algorithm in ("botsort", "boosttrack", "occluboost"):
        return bool(params.get("use_cmc", True))
    return True


BOXMOT_ALGORITHMS: dict[str, BoxMotAlgorithm] = {
    "bytetrack": BoxMotAlgorithm(
        "boxmot.trackers.bbox.bytetrack.ByteTrack",
        {
            "min_conf": 0.1,
            "track_thresh": 0.45,
            "match_thresh": 0.8,
            "track_buffer": 25,
            "frame_rate": 30,
        },
    ),
    "botsort": BoxMotAlgorithm(
        "boxmot.trackers.bbox.botsort.BotSort",
        {
            "track_high_thresh": 0.5,
            "track_low_thresh": 0.1,
            "new_track_thresh": 0.6,
            "track_buffer": 30,
            "match_thresh": 0.8,
            "proximity_thresh": 0.5,
            "appearance_thresh": 0.25,
            "use_cmc": True,
            "cmc_method": "ecc",
            "frame_rate": 30,
            "fuse_first_associate": False,
            "with_reid": True,
            "second_match_thresh": 0.5,
            "unconfirmed_match_thresh": 0.7,
            "unconfirmed_emb_scale": 2.0,
            "removed_stracks_buffer": 100,
        },
        "with_reid",
    ),
    "ocsort": BoxMotAlgorithm(
        "boxmot.trackers.bbox.ocsort.OcSort",
        {
            "min_conf": 0.1,
            "delta_t": 3,
            "inertia": 0.2,
            "use_byte": False,
            "Q_xy_scaling": 0.01,
            "Q_s_scaling": 0.0001,
        },
    ),
    "strongsort": BoxMotAlgorithm(
        "boxmot.trackers.bbox.strongsort.StrongSort",
        {
            "min_conf": 0.1,
            "max_cos_dist": 0.2,
            "max_iou_dist": 0.7,
            "n_init": 3,
            "nn_budget": 100,
            "mc_lambda": 0.98,
            "ema_alpha": 0.9,
        },
        "always",
    ),
    "deepocsort": BoxMotAlgorithm(
        "boxmot.trackers.bbox.deepocsort.DeepOcSort",
        {
            "delta_t": 3,
            "inertia": 0.2,
            "w_association_emb": 0.5,
            "alpha_fixed_emb": 0.95,
            "aw_param": 0.5,
            "embedding_off": False,
            "cmc_off": False,
            "aw_off": False,
            "Q_xy_scaling": 0.01,
            "Q_s_scaling": 0.0001,
        },
        "unless_embedding_off",
    ),
    "sfsort": BoxMotAlgorithm(
        "boxmot.trackers.bbox.sfsort.SFSORT",
        {
            "high_th": 0.6,
            "match_th_first": 0.67,
            "new_track_th": 0.7,
            "low_th": 0.1,
            "match_th_second": 0.3,
            "dynamic_tuning": False,
            "cth": 0.5,
            "high_th_m": 0.0,
            "new_track_th_m": 0.0,
            "match_th_first_m": 0.0,
            "obb_theta_damping": 0.8,
            "marginal_timeout": 0,
            "central_timeout": 0,
            "frame_width": None,
            "frame_height": None,
            "horizontal_margin": None,
            "vertical_margin": None,
        },
        excluded_common=frozenset({"det_thresh"}),
    ),
    "hybridsort": BoxMotAlgorithm(
        "boxmot.trackers.bbox.hybridsort.HybridSort",
        {
            "cmc_method": "ecc",
            "with_reid": True,
            "low_thresh": 0.1,
            "delta_t": 3,
            "inertia": 0.05,
            "use_byte": True,
            "longterm_bank_length": 30,
            "alpha": 0.9,
            "adapfs": False,
            "track_thresh": 0.5,
            "EG_weight_high_score": 4.6,
            "EG_weight_low_score": 1.3,
            "TCM_first_step": True,
            "TCM_byte_step": True,
            "TCM_byte_step_weight": 1.0,
            "high_score_matching_thresh": 0.7,
            "with_longterm_reid": True,
            "longterm_reid_weight": 0.0,
            "with_longterm_reid_correction": True,
            "longterm_reid_correction_thresh": 0.4,
            "longterm_reid_correction_thresh_low": 0.4,
            "dataset": "",
        },
        "with_any_reid",
    ),
    "boosttrack": BoxMotAlgorithm(
        "boxmot.trackers.bbox.boosttrack.BoostTrack",
        {
            "use_cmc": True,
            "min_box_area": 10,
            "aspect_ratio_thresh": 1.6,
            "cmc_method": "ecc",
            "lambda_iou": 0.5,
            "lambda_mhd": 0.25,
            "lambda_shape": 0.25,
            "use_dlo_boost": True,
            "use_duo_boost": True,
            "dlo_boost_coef": 0.65,
            "s_sim_corr": False,
            "use_rich_s": False,
            "use_sb": False,
            "use_vt": False,
            "with_reid": False,
            "adaptive_kf": False,
        },
        "with_reid",
    ),
    "occluboost": BoxMotAlgorithm(
        "boxmot.trackers.bbox.occluboost.OccluBoost",
        {
            "use_cmc": True,
            "min_box_area": 10,
            "aspect_ratio_thresh": 1.6,
            "cmc_method": "ecc",
            "lambda_iou": 0.5,
            "lambda_mhd": 0.25,
            "lambda_shape": 0.25,
            "use_dlo_boost": True,
            "use_duo_boost": True,
            "dlo_boost_coef": 0.65,
            "s_sim_corr": False,
            "use_rich_s": False,
            "use_sb": False,
            "use_vt": False,
            "with_reid": False,
            "recovery_appearance_thresh": 0.99,
            "recovery_iou_thresh": 0.1,
            "recovery_max_age": 1,
            "feat_alpha": 0.95,
            "track_low_thresh": 0.1,
            "second_iou_thresh": 0.6,
            "second_appearance_thresh": 0.5,
            "second_pass_max_age": 1,
            "second_pass_min_hits": 3,
            "use_second_pass": False,
            "new_track_thresh": 0.6,
            "confirm_hits": 2,
            "instant_confirm_thresh": 0.7,
            "tentative_max_age": 1,
            "duplicate_iou_thresh": 0.85,
            "ams_enabled": True,
            "ams_alpha0": 0.4,
            "ams_threshold": 0.5,
            "ams_buffer_size": 30,
            "ams_shrink_ratio": 0.75,
            "lambda_emb_multiplier": 1.5,
            "gta_enabled": True,
            "gta_appearance_thresh": 0.5,
            "gta_min_track_length": 5,
            "gta_smooth_tau": 5.0,
            "gta_interpolate": True,
            "gta_max_gap": 60,
            "adaptive_kf": False,
        },
        "with_reid",
    ),
}


def get_algorithm(name: str) -> BoxMotAlgorithm:
    try:
        return BOXMOT_ALGORITHMS[name]
    except KeyError as exc:
        raise RuntimeError(
            f"unsupported tracker algorithm '{name}' — expected one of {', '.join(BOXMOT_ALGORITHMS)}"
        ) from exc


def resolve_params(name: str, common: dict, configured: dict) -> dict[str, Any]:
    spec = get_algorithm(name)
    allowed_common = set(COMMON_DEFAULTS)
    unknown_common = set(common) - allowed_common
    if unknown_common:
        raise RuntimeError(
            f"tracker common config has unknown key(s): {', '.join(sorted(unknown_common))}"
        )
    allowed_algorithm = allowed_common | set(spec.defaults)
    unknown_algorithm = set(configured) - allowed_algorithm
    if unknown_algorithm:
        raise RuntimeError(
            f"tracker algorithm '{name}' has unknown key(s): {', '.join(sorted(unknown_algorithm))}"
        )
    params = {**COMMON_DEFAULTS, **spec.defaults, **common, **configured}
    for key in spec.excluded_common:
        params.pop(key, None)
    _validate_types(name, params, {**COMMON_DEFAULTS, **spec.defaults})
    return params


def _validate_types(
    name: str, values: dict[str, Any], defaults: dict[str, Any]
) -> None:
    for key, value in values.items():
        default = defaults[key]
        if default is None or value is None:
            continue
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, int) and (not isinstance(value, bool))
        elif isinstance(default, float):
            valid = isinstance(value, (int, float)) and (not isinstance(value, bool))
        else:
            valid = isinstance(value, type(default))
        if not valid:
            raise RuntimeError(
                f"tracker algorithm '{name}' key '{key}' expects {type(default).__name__}, got {type(value).__name__}"
            )
