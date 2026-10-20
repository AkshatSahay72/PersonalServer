"""
Controller Persistence Stores
=============================
Decoupled, atomic, thread-safe JSON stores for cluster state persistence.
"""

from controller.stores.base import (
    DB_LOCK,
    ONBOARDING_LOCK,
    APPS_LOCK,
    atomic_save_json,
    bump_state_version,
    set_state_version_bumper,
)
from controller.stores.nodes_store import load_nodes_db, save_nodes_db
from controller.stores.jobs_store import load_jobs_db, save_jobs_db
from controller.stores.apps_store import load_apps_db, save_apps_db
from controller.stores.onboarding_store import load_onboarding_codes_db, save_onboarding_codes_db
from controller.stores.shares_store import (
    load_shares_db,
    save_shares_db,
    get_share_by_id,
    find_active_share,
    create_share,
    revoke_share,
)

__all__ = [
    "DB_LOCK",
    "ONBOARDING_LOCK",
    "APPS_LOCK",
    "atomic_save_json",
    "bump_state_version",
    "set_state_version_bumper",
    "load_nodes_db",
    "save_nodes_db",
    "load_jobs_db",
    "save_jobs_db",
    "load_apps_db",
    "save_apps_db",
    "load_onboarding_codes_db",
    "save_onboarding_codes_db",
    "load_shares_db",
    "save_shares_db",
    "get_share_by_id",
    "find_active_share",
    "create_share",
    "revoke_share",
]
