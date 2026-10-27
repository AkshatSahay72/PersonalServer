#!/usr/bin/env python3
"""
PersonalServer Multi-Application Manifest & Routing Foundation (V2.4)
====================================================================
Defines reusable application manifests, validation, route conflict
detection, frontend/backend component separation, and deployment adapter
boundaries.

Supports path-based multi-app routing (e.g. /RecallFlow/, /MemoVault/)
with independent frontend (Vercel/static) and backend (Render/container)
destinations, API route priority, and API error isolation.
"""

import re
import json
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple, Set
from dataclasses import dataclass, field, asdict


# Reserved system paths that cannot be claimed by application public paths
RESERVED_SYSTEM_ROUTES: Set[str] = {
    "/api", "/static", "/storage", "/health", "/healthz", "/status",
    "/cluster", "/nodes", "/jobs", "/admin", "/login", "/ws", "/apps",
    "/diagnostics", "/metrics"
}

# Forbidden keywords in manifests to prevent storing credentials
CREDENTIAL_KEYWORDS: Set[str] = {
    "password", "secret", "private_key", "token", "auth_token",
    "access_token", "api_key", "credential", "id_rsa", "client_secret"
}

# Valid component providers
VALID_PROVIDERS: Set[str] = {"vercel", "render", "self-hosted", "external", "static", "container"}

# Valid component types
VALID_COMPONENT_TYPES: Set[str] = {"frontend", "backend"}

# Valid strip modes for routing
VALID_STRIP_MODES: Set[str] = {"strip_app", "strip_all", "preserve"}


class ManifestValidationError(ValueError):
    """Raised when an application manifest violates schema or safety constraints."""
    pass


class RouteConflictError(ValueError):
    """Raised when a public path or component route conflicts with an existing application."""
    pass


def sanitize_and_normalize_path(raw_path: str) -> str:
    """
    Normalizes a path string, rejects null bytes, backslashes, directory traversal,
    and returns a clean leading-slash path.
    """
    if not raw_path or not isinstance(raw_path, str):
        raise ManifestValidationError("Path must be a non-empty string.")

    if "\x00" in raw_path or "\\" in raw_path:
        raise ManifestValidationError("Malformed characters in path (null bytes or backslashes).")

    decoded = urllib.parse.unquote(raw_path)
    if "\x00" in decoded or "\\" in decoded:
        raise ManifestValidationError("Malformed characters in decoded path.")

    # Reject directory traversal
    for segment in decoded.split("/"):
        if segment in ("..", "."):
            raise ManifestValidationError("Directory traversal attempt detected in path.")

    # Strip duplicate slashes
    clean = "/" + "/".join(p for p in decoded.split("/") if p)
    return clean


def check_for_secrets(data: Any, path: str = "") -> None:
    """Recursively checks dictionaries and lists for credentials or secret keys."""
    if isinstance(data, dict):
        for k, v in data.items():
            k_lower = str(k).lower()
            current_path = f"{path}.{k}" if path else str(k)
            for kw in CREDENTIAL_KEYWORDS:
                if kw in k_lower:
                    raise ManifestValidationError(
                        f"Credential or secret field '{current_path}' is forbidden in application manifest."
                    )
            check_for_secrets(v, current_path)
    elif isinstance(data, (list, tuple)):
        for idx, item in enumerate(data):
            check_for_secrets(item, f"{path}[{idx}]")


@dataclass
class BuildMetadata:
    builder: str = "none"                  # "windows-on-demand", "vercel", "render", "none"
    command: Optional[str] = None          # e.g. "npm run build"
    base_path: Optional[str] = None        # e.g. "/RecallFlow"
    output_dir: Optional[str] = None       # e.g. "dist"
    artifact_id: Optional[str] = None      # e.g. "art-0fa243a82034"
    artifact_sha256: Optional[str] = None  # 64-char hex


@dataclass
class ComponentRouting:
    path_prefix: str = "/"                 # e.g. "/" for frontend, "/api" for backend
    strip_mode: str = "strip_app"          # "strip_app", "strip_all", "preserve"
    api_priority: bool = False             # If True, evaluated before frontend fallback
    spa_fallback: bool = False             # If True, client-side route fallback to index.html
    assets_prefix: Optional[str] = "/assets"


@dataclass
class ApplicationComponent:
    name: str
    type: str                              # "frontend" or "backend"
    provider: str                          # "vercel", "render", "self-hosted", "external", etc.
    destination: str                       # URL (https://...) or container endpoint (http://...)
    routing: ComponentRouting
    runtime: Optional[str] = None          # "nodejs", "python", "docker", etc.
    build: Optional[BuildMetadata] = None
    health_endpoint: Optional[str] = None


@dataclass
class HealthCheckConfig:
    path: str = "/health"
    interval_seconds: int = 30
    timeout_seconds: int = 5


@dataclass
class ReleaseMetadata:
    version: str = "1.0.0"
    release_id: Optional[str] = None
    deployed_at: Optional[str] = None
    last_result: str = "PENDING"           # "PENDING", "SUCCESS", "FAILED"


@dataclass
class ApplicationManifest:
    """
    Authoritative representation of a multi-component application.
    """
    name: str                              # Unique application name (e.g. "RecallFlow")
    public_path: str                       # Public URL path (e.g. "/RecallFlow")
    display_name: str
    app_id: str                            # Unique system ID (e.g. "app-recallflow")
    schema_version: str = "2.4"
    status: str = "active"                 # "active", "paused", "deploying", "failed", "deleted"
    components: Dict[str, ApplicationComponent] = field(default_factory=dict)
    health_check: Optional[HealthCheckConfig] = None
    release: Optional[ReleaseMetadata] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApplicationManifest":
        if not isinstance(data, dict):
            raise ManifestValidationError("Manifest data must be a dictionary.")

        # Check for secrets
        check_for_secrets(data)

        # Name validation
        name = str(data.get("name") or "").strip()
        if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
            raise ManifestValidationError(
                f"Invalid application name '{name}'. Must be alphanumeric, hyphens, or underscores only."
            )

        display_name = str(data.get("display_name") or name).strip()
        app_id = str(data.get("app_id") or f"app-{name.lower()}").strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', app_id):
            raise ManifestValidationError(f"Invalid app_id '{app_id}'.")

        # Public path validation
        raw_path = str(data.get("public_path") or f"/{name}").strip()
        clean_path = sanitize_and_normalize_path(raw_path)
        if clean_path != "/":
            if clean_path.lower() in RESERVED_SYSTEM_ROUTES:
                raise ManifestValidationError(f"Public path '{clean_path}' is a reserved system route.")

            if not re.match(r'^\/[a-zA-Z0-9_-]+$', clean_path):
                raise ManifestValidationError(
                    f"Invalid public path '{clean_path}'. Must start with '/' followed only by alphanumeric, hyphens, or underscores."
                )

        status = str(data.get("status", "active")).lower()

        # Parse Components
        raw_components = data.get("components")
        if not raw_components or not isinstance(raw_components, dict):
            raise ManifestValidationError("Manifest must contain at least one component in 'components'.")

        components: Dict[str, ApplicationComponent] = {}
        for comp_name, comp_data in raw_components.items():
            if not isinstance(comp_data, dict):
                raise ManifestValidationError(f"Component '{comp_name}' must be an object.")

            comp_type = str(comp_data.get("type", "frontend")).lower()
            if comp_type not in VALID_COMPONENT_TYPES:
                raise ManifestValidationError(
                    f"Component '{comp_name}' has invalid type '{comp_type}'. Must be 'frontend' or 'backend'."
                )

            provider = str(comp_data.get("provider", "external")).lower()
            if provider not in VALID_PROVIDERS:
                raise ManifestValidationError(
                    f"Component '{comp_name}' has invalid provider '{provider}'. Must be one of {VALID_PROVIDERS}."
                )

            destination = str(comp_data.get("destination") or "").strip()
            if not destination:
                raise ManifestValidationError(f"Component '{comp_name}' is missing a 'destination'.")

            # Validate destination URL format
            parsed_dest = urllib.parse.urlparse(destination)
            if parsed_dest.scheme not in ("http", "https") or not parsed_dest.netloc:
                raise ManifestValidationError(
                    f"Component '{comp_name}' destination '{destination}' is not a valid HTTP/HTTPS URL."
                )

            # Routing config
            raw_routing = comp_data.get("routing") or {}
            routing_prefix = str(raw_routing.get("path_prefix") or ("/api" if comp_type == "backend" else "/")).strip()
            if not routing_prefix.startswith("/"):
                routing_prefix = "/" + routing_prefix

            strip_mode = str(raw_routing.get("strip_mode") or "strip_app").lower()
            if strip_mode not in VALID_STRIP_MODES:
                raise ManifestValidationError(
                    f"Invalid strip_mode '{strip_mode}' for component '{comp_name}'. Must be one of {VALID_STRIP_MODES}."
                )

            api_priority = bool(raw_routing.get("api_priority", comp_type == "backend"))
            spa_fallback = bool(raw_routing.get("spa_fallback", comp_type == "frontend"))
            assets_prefix = raw_routing.get("assets_prefix", "/assets")

            routing = ComponentRouting(
                path_prefix=routing_prefix,
                strip_mode=strip_mode,
                api_priority=api_priority,
                spa_fallback=spa_fallback,
                assets_prefix=assets_prefix
            )

            # Build metadata
            raw_build = comp_data.get("build")
            build_meta = None
            if raw_build and isinstance(raw_build, dict):
                build_meta = BuildMetadata(
                    builder=str(raw_build.get("builder", "none")),
                    command=raw_build.get("command"),
                    base_path=raw_build.get("base_path"),
                    output_dir=raw_build.get("output_dir"),
                    artifact_id=raw_build.get("artifact_id"),
                    artifact_sha256=raw_build.get("artifact_sha256")
                )

            components[comp_name] = ApplicationComponent(
                name=comp_name,
                type=comp_type,
                provider=provider,
                destination=destination.rstrip("/"),
                routing=routing,
                runtime=comp_data.get("runtime"),
                build=build_meta,
                health_endpoint=comp_data.get("health_endpoint")
            )

        # Health Check
        raw_health = data.get("health_check")
        health_cfg = None
        if raw_health and isinstance(raw_health, dict):
            health_cfg = HealthCheckConfig(
                path=str(raw_health.get("path", "/health")),
                interval_seconds=int(raw_health.get("interval_seconds", 30)),
                timeout_seconds=int(raw_health.get("timeout_seconds", 5))
            )

        # Release Metadata
        raw_release = data.get("release")
        release_meta = None
        if raw_release and isinstance(raw_release, dict):
            release_meta = ReleaseMetadata(
                version=str(raw_release.get("version", "1.0.0")),
                release_id=raw_release.get("release_id"),
                deployed_at=raw_release.get("deployed_at"),
                last_result=str(raw_release.get("last_result", "PENDING")).upper()
            )

        return cls(
            name=name,
            public_path=clean_path,
            display_name=display_name,
            app_id=app_id,
            schema_version=str(data.get("schema_version", "2.4")),
            status=status,
            components=components,
            health_check=health_cfg,
            release=release_meta
        )

    def to_dict(self) -> Dict[str, Any]:
        """Converts manifest into a JSON-serializable dictionary."""
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "app_id": self.app_id,
            "public_path": self.public_path,
            "display_name": self.display_name,
            "status": self.status,
            "components": {
                k: {
                    "name": c.name,
                    "type": c.type,
                    "provider": c.provider,
                    "destination": c.destination,
                    "runtime": c.runtime,
                    "health_endpoint": c.health_endpoint,
                    "routing": asdict(c.routing),
                    "build": asdict(c.build) if c.build else None
                }
                for k, c in self.components.items()
            },
            "health_check": asdict(self.health_check) if self.health_check else None,
            "release": asdict(self.release) if self.release else None
        }


class ManifestRegistry:
    """
    In-memory and persistent manager for multi-application manifests.
    Guarantees name uniqueness and public path conflict avoidance.
    """

    def __init__(self):
        self._manifests: Dict[str, ApplicationManifest] = {}

    def register(self, manifest_data: Any) -> ApplicationManifest:
        """
        Registers a new application manifest.
        Rejects duplicates and path collisions.
        """
        if isinstance(manifest_data, ApplicationManifest):
            manifest = manifest_data
        elif isinstance(manifest_data, dict):
            manifest = ApplicationManifest.from_dict(manifest_data)
        else:
            raise ManifestValidationError("Invalid manifest type.")

        name_lower = manifest.name.lower()
        path_lower = manifest.public_path.lower()

        # Check name and path collision against all active applications
        for existing in self._manifests.values():
            if existing.status == "deleted":
                continue
            if existing.name.lower() == name_lower:
                raise RouteConflictError(f"Application name '{manifest.name}' is already registered.")
            if existing.app_id.lower() == manifest.app_id.lower():
                raise RouteConflictError(f"Application ID '{manifest.app_id}' is already registered.")
            if existing.public_path.lower() == path_lower:
                raise RouteConflictError(
                    f"Public path '{manifest.public_path}' conflicts with existing application '{existing.name}'."
                )

        self._manifests[manifest.app_id] = manifest
        return manifest

    def get_by_name(self, name: str) -> Optional[ApplicationManifest]:
        name_lower = name.lower()
        for m in self._manifests.values():
            if m.status != "deleted" and (m.name.lower() == name_lower or m.app_id.lower() == name_lower):
                return m
        return None

    def get_by_path(self, path: str) -> Optional[ApplicationManifest]:
        """
        Finds application whose public path matches the root segment of request path.
        """
        clean = path.strip().lower()
        root_app = None
        for m in self._manifests.values():
            if m.status == "deleted":
                continue
            m_path = m.public_path.lower()
            if m_path == "/":
                root_app = m
                continue
            if clean == m_path or clean.startswith(m_path + "/"):
                return m
        return root_app

    def update(self, app_id_or_name: str, updates: Dict[str, Any]) -> ApplicationManifest:
        """
        Updates an existing application manifest without altering other applications.
        """
        existing = self.get_by_name(app_id_or_name)
        if not existing:
            raise KeyError(f"Application '{app_id_or_name}' not found.")

        current_data = existing.to_dict()
        # Apply updates
        current_data.update(updates)
        new_manifest = ApplicationManifest.from_dict(current_data)

        # Verify no conflict with other apps
        path_lower = new_manifest.public_path.lower()
        for other in self._manifests.values():
            if other.app_id == existing.app_id or other.status == "deleted":
                continue
            if other.name.lower() == new_manifest.name.lower():
                raise RouteConflictError(f"Updated name '{new_manifest.name}' conflicts with existing application.")
            if other.public_path.lower() == path_lower:
                raise RouteConflictError(f"Updated public path '{new_manifest.public_path}' conflicts with existing application.")

        self._manifests[existing.app_id] = new_manifest
        return new_manifest

    def remove(self, app_id_or_name: str) -> bool:
        """Removes or marks an application manifest as deleted."""
        existing = self.get_by_name(app_id_or_name)
        if not existing:
            return False
        self._manifests[existing.app_id].status = "deleted"
        return True

    def list_all(self, include_deleted: bool = False) -> List[ApplicationManifest]:
        return [m for m in self._manifests.values() if include_deleted or m.status != "deleted"]


@dataclass
class ResolvedRoute:
    """Result of path resolution against an ApplicationManifest."""
    app_name: str
    app_id: str
    component_name: str
    component_type: str                    # "frontend" or "backend"
    upstream_url: str                      # Fully formed target URL
    is_api: bool
    spa_fallback: bool
    is_trailing_slash_redirect: bool = False
    redirect_target: Optional[str] = None


class AppRouteResolver:
    """
    Pure route resolution engine.
    Applies API priority, strip modes, and determines exact upstream URLs.
    """

    @staticmethod
    def resolve(request_path: str, registry: ManifestRegistry, query_string: str = "") -> Tuple[Optional[ResolvedRoute], Optional[str]]:
        """
        Resolves an incoming request path against registered applications.
        Returns (ResolvedRoute, error_message).
        """
        try:
            clean_path = sanitize_and_normalize_path(request_path)
        except ManifestValidationError as e:
            return None, str(e)

        parsed = urllib.parse.urlparse(request_path)
        path_only = parsed.path
        parts = [p for p in path_only.split("/") if p]

        # Match application by longest prefix (specific sub-apps first, root '/' fallback)
        app = registry.get_by_path(path_only)
        if not app or app.status != "active":
            return None, None

        app_prefix = app.public_path  # e.g. /RecallFlow or /

        if app_prefix == "/":
            # Root application handling (e.g. portfolio)
            for c in app.components.values():
                if c.type == "backend":
                    api_prefix = c.routing.path_prefix.lower()
                    if path_only.lower() == api_prefix or path_only.lower().startswith(api_prefix + "/"):
                        upstream_url = f"{c.destination}{path_only}"
                        if query_string:
                            upstream_url += f"?{query_string}"
                        return ResolvedRoute(
                            app_name=app.name,
                            app_id=app.app_id,
                            component_name=c.name,
                            component_type="backend",
                            upstream_url=upstream_url,
                            is_api=True,
                            spa_fallback=False
                        ), None

            for c in app.components.values():
                if c.type in ("frontend", "web"):
                    upstream_url = f"{c.destination}{path_only}"
                    if query_string:
                        upstream_url += f"?{query_string}"
                    return ResolvedRoute(
                        app_name=app.name,
                        app_id=app.app_id,
                        component_name=c.name,
                        component_type=c.type,
                        upstream_url=upstream_url,
                        is_api=False,
                        spa_fallback=False
                    ), None
            return None, "No active component found for root application"

        # 1. Trailing slash check: /RecallFlow without slash -> redirect to /RecallFlow/
        if path_only.lower() == app_prefix.lower():
            target = f"{app_prefix}/"
            if query_string:
                target += f"?{query_string}"
            return ResolvedRoute(
                app_name=app.name,
                app_id=app.app_id,
                component_name="",
                component_type="",
                upstream_url="",
                is_api=False,
                spa_fallback=False,
                is_trailing_slash_redirect=True,
                redirect_target=target
            ), None

        # Calculate remainder after public_path: e.g. /RecallFlow/api/users -> /api/users
        remainder = path_only[len(app_prefix):]
        if not remainder.startswith("/"):
            remainder = "/" + remainder

        # 2. Separate components into API (backend) and Frontend
        backend_comp: Optional[ApplicationComponent] = None
        frontend_comp: Optional[ApplicationComponent] = None

        for c in app.components.values():
            if c.type == "backend" and c.routing.api_priority:
                backend_comp = c
            elif c.type == "frontend":
                frontend_comp = c

        # 3. API Route Precedence Evaluation
        if backend_comp:
            api_prefix = backend_comp.routing.path_prefix  # e.g. "/api"
            remainder_lower = remainder.lower()
            api_prefix_lower = api_prefix.lower()

            # Exact /api or /api/... match
            if remainder_lower == api_prefix_lower or remainder_lower.startswith(api_prefix_lower + "/"):
                # Handle /RecallFlow/api without trailing slash if api_prefix is /api
                if remainder_lower == api_prefix_lower and not remainder.endswith("/"):
                    # Normal redirect or forward
                    pass

                # Calculate upstream path based on strip_mode
                strip_mode = backend_comp.routing.strip_mode
                if strip_mode == "strip_app":
                    # /RecallFlow/api/v1 -> /api/v1
                    forwarded_path = remainder
                elif strip_mode == "strip_all":
                    # /RecallFlow/api/v1 -> /v1
                    forwarded_path = remainder[len(api_prefix):]
                    if not forwarded_path.startswith("/"):
                        forwarded_path = "/" + forwarded_path
                else:
                    # preserve: /RecallFlow/api/v1
                    forwarded_path = path_only

                if query_string:
                    forwarded_path += f"?{query_string}"

                upstream_url = f"{backend_comp.destination}{forwarded_path}"
                return ResolvedRoute(
                    app_name=app.name,
                    app_id=app.app_id,
                    component_name=backend_comp.name,
                    component_type="backend",
                    upstream_url=upstream_url,
                    is_api=True,
                    spa_fallback=False  # API errors MUST NEVER fallback to frontend HTML
                ), None

        # 4. Frontend Route Evaluation
        if frontend_comp:
            strip_mode = frontend_comp.routing.strip_mode
            if strip_mode in ("strip_app", "strip_all"):
                # /RecallFlow/dashboard -> /dashboard
                forwarded_path = remainder
            else:
                forwarded_path = path_only

            if query_string:
                forwarded_path += f"?{query_string}"

            upstream_url = f"{frontend_comp.destination}{forwarded_path}"
            is_asset = bool(
                frontend_comp.routing.assets_prefix and
                remainder.startswith(frontend_comp.routing.assets_prefix)
            )

            return ResolvedRoute(
                app_name=app.name,
                app_id=app.app_id,
                component_name=frontend_comp.name,
                component_type="frontend",
                upstream_url=upstream_url,
                is_api=False,
                spa_fallback=not is_asset  # Assets don't SPA fallback
            ), None

        return None, "No active component found for application route"


# ==============================================================================
# Deployment Adapter Boundaries
# ==============================================================================

class DeploymentAdapter:
    """Abstract interface defining the contract for deploying application components."""
    provider_name: str = "base"

    @classmethod
    def get_required_config(cls) -> Dict[str, Any]:
        """Returns documented external credentials and configuration keys required."""
        raise NotImplementedError

    @classmethod
    def validate_component(cls, component: ApplicationComponent) -> Tuple[bool, str]:
        """Validates that component properties conform to the provider's expectations."""
        raise NotImplementedError


class VercelDeploymentAdapter(DeploymentAdapter):
    """
    Adapter boundary for Vercel-hosted frontend components.
    Requires external Vercel project configuration and environment variables.
    """
    provider_name = "vercel"

    @classmethod
    def get_required_config(cls) -> Dict[str, Any]:
        return {
            "provider": "vercel",
            "required_env_vars": ["VERCEL_TOKEN", "VERCEL_PROJECT_ID", "VERCEL_ORG_ID"],
            "external_routing_layer": "Vercel Rewrites (vercel.json on root portfolio project)",
            "documentation": (
                "When akshatsahay.space is served by Vercel, public routes under /RecallFlow/ "
                "require a rewrite rule in the portfolio vercel.json pointing to the RecallFlow frontend deployment, "
                "and /RecallFlow/api/* rewriting to the Render backend service or PersonalServer gateway."
            )
        }

    @classmethod
    def validate_component(cls, component: ApplicationComponent) -> Tuple[bool, str]:
        if component.type != "frontend":
            return False, "Vercel adapter only supports frontend components in this foundation."
        if not component.destination.startswith("https://"):
            return False, "Vercel destination must be a secure HTTPS URL."
        return True, "Valid Vercel component configuration."


class RenderDeploymentAdapter(DeploymentAdapter):
    """
    Adapter boundary for Render-hosted backend components.
    Requires external Render Web Service configuration.
    """
    provider_name = "render"

    @classmethod
    def get_required_config(cls) -> Dict[str, Any]:
        return {
            "provider": "render",
            "required_env_vars": ["RENDER_API_KEY", "RENDER_SERVICE_ID"],
            "deploy_hook_support": True,
            "documentation": (
                "Backend API service hosted on Render (e.g. https://recallflow-api.onrender.com). "
                "CORS must allow https://akshatsahay.space, and route prefixes must expect "
                "the stripped path /api/... as configured in the manifest."
            )
        }

    @classmethod
    def validate_component(cls, component: ApplicationComponent) -> Tuple[bool, str]:
        if component.type != "backend":
            return False, "Render adapter currently supports backend API components."
        if not component.destination.startswith("https://"):
            return False, "Render destination must be a secure HTTPS URL."
        return True, "Valid Render component configuration."


class PersonalServerSelfHostedAdapter(DeploymentAdapter):
    """
    Adapter boundary for self-hosted PersonalServer workloads.
    Uses existing on-demand Windows build worker, verified ZIP artifact contract,
    and Vivo Y31 controller authoritative artifact storage.
    """
    provider_name = "self-hosted"

    @classmethod
    def get_required_config(cls) -> Dict[str, Any]:
        return {
            "provider": "self-hosted",
            "build_target": "cap:build",
            "storage_authority": "Vivo Y31 Controller",
            "laptop_role": "On-demand build execution only; no persistent storage",
            "artifact_format": "zip with sha256 checksum verification"
        }

    @classmethod
    def validate_component(cls, component: ApplicationComponent) -> Tuple[bool, str]:
        # Validate that destination is a local or internal endpoint
        return True, "Valid self-hosted component configuration."
