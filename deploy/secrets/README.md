Put deployment secrets here (this directory's contents are git-ignored except this file):

- m365c_cert.pem                 PEM: private key + certificate of the Netwatch app registration
                                 (only if a tenant uses the default certificate)
- tenants/<customer>_client_secret  one client secret per tenant (client_secret auth), no
                                 trailing text; owned by uid 10001, mode 0400
- grafana_admin_password         Grafana admin password (single line)
- alertmanager_smtp_password     SMTP password for Alertmanager
- alertmanager_webhook_contoso   Webhook URL for the example customer receiver
