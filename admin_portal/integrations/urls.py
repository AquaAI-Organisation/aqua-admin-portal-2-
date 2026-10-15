"""Machine-to-machine integration routes.

Mounted at the project root, NOT under the admin portal's authenticated
prefix: the CRM authenticates with a signature, not a session, and the path
must be exactly ``/api/integrations/v2/crm-events`` or the CRM refuses to
send (``aquaV2IntegrationDelivery.ts``:57-59).
"""
from django.urls import path

from . import views

app_name = "v2_integrations"

urlpatterns = [
    path("api/integrations/v2/crm-events", views.crm_events, name="crm_events"),
]
