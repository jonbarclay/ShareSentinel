# Pin to Debian 12 (bookworm) because the Microsoft PowerShell apt repo signing
# key uses SHA1, which Debian 13 (trixie) refuses post-2026-02 — the default
# `python:3.12-slim` rolled to trixie and apt rejects the MS repo on it.
FROM python:3.12-slim-bookworm

WORKDIR /app

# PowerShell + PnP module for CWS PowerShell-based scans/remediation
# (DisableCompanyWideSharingLinks cannot be reliably set via the Python/CSOM
# tenant admin API at large tenant scale; PnP cmdlets handle it correctly.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates gnupg apt-transport-https postgresql-client && \
    wget -q "https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb" -O /tmp/ms-prod.deb && \
    dpkg -i /tmp/ms-prod.deb && rm /tmp/ms-prod.deb && \
    apt-get update && apt-get install -y --no-install-recommends powershell && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install PnP.PowerShell + ThreadJob system-wide so the non-root appuser
# can load them.
RUN pwsh -NoProfile -Command "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; \
        Install-Module -Name PnP.PowerShell -RequiredVersion 3.0.0 -Scope AllUsers -Force -AllowClobber; \
        Install-Module -Name ThreadJob -RequiredVersion 2.0.0 -Scope AllUsers -Force"

COPY services/lifecycle-cron/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install shared package
COPY services/shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared

COPY services/lifecycle-cron/app/ ./app/

# Non-root user
RUN useradd -m -r appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
