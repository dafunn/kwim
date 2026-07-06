"""KWIM service configuration - the single inventory of every setting.

Two surfaces, by concern:

  * Secrets + connection endpoints stay discrete env vars (host/port/user/db/vhost
    as plain env; passwords/keys as their own env vars from mounted secret
    files). We deliberately do not build URL DSNs with inline passwords -
    base64/random passwords contain `+ / = @` which corrupt a URL's authority
    parsing. Passing params as keyword args sidesteps URL-encoding entirely.
    These have no YAML home: they come from k8s Secrets, never a checked-in file.

  * Everything else is a tunable, declared in `kwim.defaults.yaml` and
    overridable, in precedence order:
        env var  >  KWIM_CONFIG (user YAML)  >  kwim.defaults.yaml
    So a deployer customizes one YAML file, k8s can still pin any key via the
    ConfigMap env block, and the defaults are the safety net.
"""
import json
import os
from dataclasses import dataclass, field

import yaml

# --- layered load: defaults <- user KWIM_CONFIG (deep-merged) --------
_DEFAULTS_PATH = os.path.join(os.path.dirname(__file__), "kwim.defaults.yaml")


def _load_yaml(path: str) -> dict:
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_CFG = _load_yaml(_DEFAULTS_PATH)
_USER_CONFIG = os.environ.get("KWIM_CONFIG", "")
if _USER_CONFIG:
    _CFG = _deep_merge(_CFG, _load_yaml(_USER_CONFIG))


def _dig(dotted: str, fallback):
    cur = _CFG
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return fallback
        cur = cur[part]
    return cur


def _cfg(dotted: str, env: str, cast, fallback):
    """env var (a string) overrides the merged-YAML value, which overrides the
    in-code fallback (used only if the defaults file is missing/incomplete)."""
    raw = os.environ.get(env)
    if raw is not None and raw != "":
        return cast(raw)
    return _dig(dotted, fallback)


def _as_bool(raw) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _cfg_bool(dotted: str, env: str, fallback: bool) -> bool:
    raw = os.environ.get(env)
    if raw is not None and raw != "":
        return _as_bool(raw)
    return bool(_dig(dotted, fallback))


def _cfg_list(dotted: str, env: str, fallback: list) -> list:
    raw = os.environ.get(env)
    if raw is not None and raw != "":
        return list(json.loads(raw))          # env override carries JSON
    v = _dig(dotted, fallback)
    return list(v) if v is not None else list(fallback)


def _cfg_dict(dotted: str, env: str, fallback: dict) -> dict:
    raw = os.environ.get(env)
    if raw is not None and raw != "":
        return dict(json.loads(raw))
    v = _dig(dotted, fallback)
    return dict(v) if isinstance(v, dict) else dict(fallback)


# Collections are precomputed (frozen dataclass can't take mutable defaults).
_LANG_BY_EXT = _cfg_dict("codegraph.discovery.lang_by_ext", "KWIM_CG_LANG_BY_EXT",
                         {".py": "python"})
_SKIP_DIRS = frozenset(_cfg_list(
    "codegraph.discovery.skip_dirs", "KWIM_CG_SKIP_DIRS",
    [".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
     ".mypy_cache", ".ruff_cache", "dist", "build", ".tox", "tests", "test"]))
_IGNORE_FILES = tuple(_cfg_list(
    "codegraph.discovery.ignore_files", "KWIM_CG_IGNORE_FILES",
    [".gitignore", ".cgignore"]))


@dataclass(frozen=True)
class Settings:
    # ============ Secrets + connection endpoints (env-only; from k8s Secrets) ============
    # --- Postgres (episodic + commit_log) ---
    pg_host: str = os.environ.get("KWIM_PG_HOST", "")
    pg_port: int = int(os.environ.get("KWIM_PG_PORT", "5432"))
    pg_db: str = os.environ.get("KWIM_PG_DB", "kwim")
    pg_user: str = os.environ.get("KWIM_PG_USER", "kwim_user")
    pg_password: str = os.environ.get("KWIM_PG_PASSWORD", "")

    # --- FalkorDB (graph + vector + working KV) ---
    falkor_host: str = os.environ.get("KWIM_FALKOR_HOST", "")
    falkor_port: int = int(os.environ.get("KWIM_FALKOR_PORT", "6379"))
    falkor_password: str = os.environ.get("KWIM_FALKOR_PASSWORD", "")

    # --- RabbitMQ (/kwim vhost) ---
    rmq_host: str = os.environ.get("KWIM_RMQ_HOST", "")
    rmq_port: int = int(os.environ.get("KWIM_RMQ_PORT", "5672"))
    rmq_user: str = os.environ.get("KWIM_RMQ_USER", "kwim")
    rmq_vhost: str = os.environ.get("KWIM_RMQ_VHOST", "/kwim")
    rmq_password: str = os.environ.get("KWIM_RMQ_PASSWORD", "")

    # --- Embedder (TEI / semantic memory) endpoint ---
    embedder_url: str = os.environ.get("KWIM_EMBEDDER_URL", "")

    # --- Auth / capability allowlists (keys are secrets; modes are env) ---
    api_key_source: str = os.environ.get("KWIM_API_KEY_SOURCE", "env")
    # NB: KWIM_API_KEYS ("key:team,key:team") is read directly by auth.py, not
    # snapshotted here - it is a runtime-provisioned secret whose key map is built at
    # import and must reflect the live env (a frozen snapshot would miss late binding).
    # Comma-separated key-id prefixes (first 6 chars of the bearer key) permitted to
    # promote/seed. Empty = no promotions allowed (fail-closed). Proper RBAC later.
    promote_keys: str = os.environ.get("KWIM_PROMOTE_KEYS", "")
    # Comma-separated key-id prefixes permitted to use /v1/review/*. Empty = fail-closed.
    review_keys: str = os.environ.get("KWIM_REVIEW_KEYS", "")

    # --- Human-review surface - secrets + this service's own URL ---
    # Incoming webhook URL for review notifications. Unset = no notifications.
    mm_webhook_url: str = os.environ.get("KWIM_MM_WEBHOOK_URL", "")
    # Shared secret embedded in Mattermost action-button callbacks (hmac-compared).
    # Unset means POST /v1/review/mm-action always 403s.
    mm_action_secret: str = os.environ.get("KWIM_MM_ACTION_SECRET", "")
    # This service's own externally-reachable base URL, used to build the mm-action
    # callback URL. Unset = notify-only mode (no buttons).
    service_url: str = os.environ.get("KWIM_SERVICE_URL", "")

    # --- Telemetry ---
    # NB: OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_SERVICE_NAME (standard OTEL env names,
    # no KWIM_ prefix) are read directly by otel.py and the OTLP exporter - live env,
    # not snapshotted (configure() may run before the value is bound). Listed here so
    # config.py stays the one inventory of every setting the service consumes.

    # ===================== Tunables (kwim.defaults.yaml + env) =====================
    # --- Gate (dedup, contradiction screen, evidence integrity) ---
    gate_auto_commit_threshold: int = _cfg("gate.auto_commit_threshold", "KWIM_GATE_THRESHOLD", int, 3)
    gate_dup_distance: float = _cfg("gate.dup_distance", "KWIM_GATE_DUP_DIST", float, 0.05)
    gate_review_distance: float = _cfg("gate.review_distance", "KWIM_GATE_REVIEW_DIST", float, 0.25)
    gate_verify_enabled: bool = _cfg_bool("gate.verify_enabled", "KWIM_GATE_VERIFY", True)
    gate_summary_max: int = _cfg("gate.summary_max", "KWIM_GATE_SUMMARY_MAX", int, 500)

    # --- Decay / freshness ---
    halflife_slow_days: float = _cfg("decay.halflife_slow_days", "KWIM_DECAY_HALFLIFE_SLOW", float, 90)
    halflife_fast_hours: float = _cfg("decay.halflife_fast_hours", "KWIM_DECAY_HALFLIFE_FAST", float, 48)

    # --- Embedder model shape ---
    embed_dim: int = _cfg("embedder.dim", "KWIM_EMBED_DIM", int, 384)
    embed_timeout_s: float = _cfg("embedder.timeout_s", "KWIM_EMBED_TIMEOUT", float, 10.0)

    # --- Retrieval / warm-start sizing ---
    episodic_max_limit: int = _cfg("retrieval.episodic_max_limit", "KWIM_EPISODIC_MAX_LIMIT", int, 2000)
    episodic_default_limit: int = _cfg("retrieval.episodic_default_limit", "KWIM_EPISODIC_DEFAULT_LIMIT", int, 500)
    rule_query_limit: int = _cfg("retrieval.rule_query_limit", "KWIM_RULE_QUERY_LIMIT", int, 20)
    fact_query_limit: int = _cfg("retrieval.fact_query_limit", "KWIM_FACT_QUERY_LIMIT", int, 20)
    rule_scan_limit: int = _cfg("retrieval.rule_scan_limit", "KWIM_RULE_SCAN_LIMIT", int, 200)
    code_slot_limit: int = _cfg("retrieval.code_slot_limit", "KWIM_CODE_SLOT_LIMIT", int, 8)

    # --- Rebuild ---
    embed_batch: int = _cfg("rebuild.embed_batch", "KWIM_EMBED_BATCH", int, 32)

    # --- Messaging / graph names ---
    rmq_exchange: str = _cfg("messaging.rmq_exchange", "KWIM_RMQ_EXCHANGE", str, "kwim")
    universe_graph: str = _cfg("messaging.universe_graph", "KWIM_UNIVERSE_GRAPH", str, "universe")

    # --- Code graph (+T) distillation + resolution cascade ---
    cg_min_fan_in: int = _cfg("codegraph.min_fan_in", "KWIM_CG_MIN_FAN_IN", int, 5)
    cg_min_confidence: float = _cfg("codegraph.min_confidence", "KWIM_CG_MIN_CONFIDENCE", float, 0.75)
    cg_community_min_confidence: float = _cfg("codegraph.community_min_confidence", "KWIM_CG_COMMUNITY_MIN_CONFIDENCE", float, 0.5)
    cg_arch_top_hubs: int = _cfg("codegraph.arch_top_hubs", "KWIM_CG_ARCH_TOP_HUBS", int, 8)
    cg_min_bridged_communities: int = _cfg("codegraph.min_bridged_communities", "KWIM_CG_MIN_BRIDGED_COMMUNITIES", int, 3)
    cg_interface_query_limit: int = _cfg("codegraph.interface_query_limit", "KWIM_CG_INTERFACE_QUERY_LIMIT", int, 100)
    cg_conf_import_map: float = _cfg("codegraph.resolution.import_map", "KWIM_CG_CONF_IMPORT_MAP", float, 0.95)
    cg_conf_import_map_suffix: float = _cfg("codegraph.resolution.import_map_suffix", "KWIM_CG_CONF_IMPORT_MAP_SUFFIX", float, 0.85)
    cg_conf_same_class: float = _cfg("codegraph.resolution.same_class", "KWIM_CG_CONF_SAME_CLASS", float, 0.92)
    cg_conf_same_module: float = _cfg("codegraph.resolution.same_module", "KWIM_CG_CONF_SAME_MODULE", float, 0.90)
    cg_conf_qualified_suffix: float = _cfg("codegraph.resolution.qualified_suffix", "KWIM_CG_CONF_QUALIFIED_SUFFIX", float, 0.90)
    cg_conf_unique_name: float = _cfg("codegraph.resolution.unique_name", "KWIM_CG_CONF_UNIQUE_NAME", float, 0.75)
    cg_conf_suffix_match: float = _cfg("codegraph.resolution.suffix_match", "KWIM_CG_CONF_SUFFIX_MATCH", float, 0.55)
    cg_conf_fuzzy_single: float = _cfg("codegraph.resolution.fuzzy_single", "KWIM_CG_CONF_FUZZY_SINGLE", float, 0.40)
    cg_conf_fuzzy_multi: float = _cfg("codegraph.resolution.fuzzy_multi", "KWIM_CG_CONF_FUZZY_MULTI", float, 0.30)
    cg_reg_max_candidates: int = _cfg("codegraph.resolution.max_candidates", "KWIM_CG_REG_MAX_CANDIDATES", int, 20)

    # --- Code graph (+T) source discovery (collections; env override = JSON) ---
    cg_lang_by_ext: dict = field(default_factory=lambda: dict(_LANG_BY_EXT))
    cg_skip_dirs: frozenset = _SKIP_DIRS
    cg_ignore_files: tuple = _IGNORE_FILES


settings = Settings()
