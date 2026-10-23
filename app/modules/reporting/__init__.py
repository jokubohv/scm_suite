"""FS-15 bounded context: reporting read models (no base tables; 21 read-only views + reporting API)."""

from app.modules.reporting.catalogue import ENDPOINTS, REPORT_ALIASES, VIEW_NAMES, VIEWS, VIEWS_BY_NAME
from app.modules.reporting.service import ReportingError, ReportService
from app.modules.reporting.views import ViewSpec

__all__ = ["ENDPOINTS", "REPORT_ALIASES", "VIEWS", "VIEW_NAMES", "VIEWS_BY_NAME", "ReportService",
           "ReportingError", "ViewSpec"]
