from leasing.config import Settings
from leasing.crm.base import ContactData, CrmAdapter, CrmError
from leasing.crm.memory import InMemoryCrm


def build_crm(settings: Settings) -> CrmAdapter:
    if settings.crm_backend == "hubspot":
        from leasing.crm.hubspot import HubSpotCrm

        return HubSpotCrm(settings.hubspot_access_token)
    return InMemoryCrm()


__all__ = ["ContactData", "CrmAdapter", "CrmError", "InMemoryCrm", "build_crm"]
