"""Check a notification service without passing message contents to Python."""
service = data.get("service", "")
if not isinstance(service, str) or service.count(".") != 1 or not service.startswith("notify."):
    raise ValueError("Expected a notify service")
domain, name = service.split(".")
output["available"] = hass.services.has_service(domain, name)
