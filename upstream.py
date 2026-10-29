"""Copilot API routing for public Enterprise and GHE data-residency hosts."""
import re
from urllib.parse import urlsplit


DEFAULT_UPSTREAM = 'https://api.enterprise.githubcopilot.com'
GHE_HOST = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com')


def validate_upstream(value):
    try:
        parts = urlsplit(value)
        host = parts.hostname or ''
        valid_host = (host == 'api.enterprise.githubcopilot.com'
                      or host.startswith('copilot-api.') and GHE_HOST.fullmatch(host[len('copilot-api.'):]))
        if (parts.scheme != 'https' or not valid_host or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None
                or parts.path not in ('', '/') or parts.query or parts.fragment):
            raise ValueError('Unsupported endpoint')
        return 'https://' + host
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError('Unsupported Copilot API URL. Use https://api.enterprise.githubcopilot.com or https://copilot-api.<enterprise>.ghe.com.') from None


def credential_hostname(args):
    hostname = None
    for index, value in enumerate(args):
        if value == '--':
            break
        if value == '--hostname' and index + 1 < len(args):
            hostname = args[index + 1]
        elif value.startswith('--hostname='):
            hostname = value.split('=', 1)[1]
    return hostname


def select_upstream(args, current=DEFAULT_UPSTREAM, hostname=None):
    current = validate_upstream(current)
    hostname = hostname if hostname is not None else credential_hostname(args)
    if hostname is not None:
        host = hostname.strip().lower().rstrip('.')
        if GHE_HOST.fullmatch(host):
            return 'https://copilot-api.' + host
        if host == 'github.com':
            return DEFAULT_UPSTREAM
    return current


def settings_upstream(settings):
    if settings.get('upstream_url'):
        return validate_upstream(settings['upstream_url'])
    # Older installs did not save an endpoint. Their original URL and helper
    # arguments are sufficient to migrate known GHE hosts without a token probe.
    current = settings.get('old', {}).get('base_url', DEFAULT_UPSTREAM)
    return select_upstream(settings.get('auth_args', []), current)
