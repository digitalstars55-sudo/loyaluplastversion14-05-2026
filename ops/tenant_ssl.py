#!/usr/bin/env python3
"""Host-only reconciliation of persisted tenant domains with the existing SAN certificate."""
import argparse
import fcntl
import json
import logging
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time

LOG = logging.getLogger('loyalup.tenant_ssl')
WEEK = 7 * 24 * 3600


def command(args, timeout=60):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def certificate_names(path):
    output = command(['/usr/bin/openssl', 'x509', '-in', str(path), '-noout', '-ext', 'subjectAltName'])
    names = set(re.findall(r'DNS:([^,\s]+)', output))
    if not names:
        raise RuntimeError('Existing certificate has no DNS SANs; refusing to replace it')
    return names


def tenant_domain(domain, base_domain):
    suffix = '.' + base_domain
    if not domain.endswith(suffix):
        return False
    label = domain[:-len(suffix)]
    return bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label))


def plan(existing, domains, config, resolve):
    added, waiting = set(), []
    for domain in sorted(set(domains) - existing):
        if not tenant_domain(domain, config['base_domain']) or domain in config['excluded_domains']:
            continue
        try:
            addresses = set(resolve(domain))
        except OSError:
            addresses = set()
        if addresses and addresses <= set(config['allowed_ips']):
            added.add(domain)
        else:
            waiting.append(domain)
    required = existing | added
    if len(required) > 100:
        raise RuntimeError('SAN limit (100) reached; retain current certificate and provision another one')
    return required, waiting


class Runtime:
    def __init__(self, config):
        self.config = config
        self.cert_dir = Path('/etc/letsencrypt/live') / config['cert_name']

    def domains(self):
        # Dedicated direct psql session, read-only; credentials stay inside the DB container.
        sql = 'SELECT domain FROM public.clients_domain ORDER BY domain;'
        output = command([
            '/usr/bin/docker', 'exec', '-e', 'PGOPTIONS=-c default_transaction_read_only=on',
            self.config['database_container'], 'sh', '-c',
            'exec psql -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
            'sh', sql,
        ])
        return [line.strip().lower().rstrip('.') for line in output.splitlines() if line.strip()]

    def names(self):
        return certificate_names(self.cert_dir / 'fullchain.pem')

    def resolve(self, domain):
        return {item[4][0] for item in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}

    def save(self, state):
        destination = Path(self.config['state_dir']) / 'state.json'
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(destination)

    def backup(self):
        destination = Path(self.config['state_dir']) / 'backups' / str(time.time_ns())
        destination.mkdir(parents=True, mode=0o700)
        shutil.copytree('/etc/nginx', destination / 'nginx', symlinks=True)
        for part in ('live', 'archive', 'renewal'):
            name = self.config['cert_name'] + ('.conf' if part == 'renewal' else '')
            source = Path('/etc/letsencrypt') / part / name
            target = destination / 'letsencrypt' / part / source.name
            target.parent.mkdir(parents=True, mode=0o700)
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
        LOG.info('Created root-only backup: %s', destination)

    def nginx_check(self):
        command(['/usr/sbin/nginx', '-t'])

    def issue(self, names):
        args = ['/usr/bin/certbot', 'certonly', '--nginx', '--non-interactive',
                '--cert-name', self.config['cert_name'], '--expand']
        for domain in sorted(names):
            args.extend(['-d', domain])
        command(args, timeout=600)

    def reload(self):
        self.nginx_check()
        command(['/usr/bin/systemctl', 'reload', 'nginx'])
        command(['/usr/bin/systemctl', 'is-active', 'nginx'])


def reconcile(config, state, runtime, now, check_only=False):
    existing = runtime.names()
    if state.get('pending_reload') and not check_only:
        expected = set(state['pending_reload'])
        if not set(state.get('previous_names', expected)) <= existing:
            raise RuntimeError('Issued certificate lost previous names; refusing nginx reload')
        if expected <= existing:
            runtime.reload()
            LOG.info('Completed pending nginx reload')
        # If issuance failed/interrupted before replacing the cert, wait for the saved backoff.
        state.pop('pending_reload')
        state.pop('previous_names', None)
        runtime.save(state)
    required, waiting = plan(existing, runtime.domains(), config, runtime.resolve)
    result = {'covered': len(existing), 'add': sorted(required - existing), 'waiting_dns': waiting}
    if check_only or required == existing:
        return result
    attempts = [entry for entry in state.get('attempts', []) if entry > now - WEEK]
    if now < state.get('retry_after', 0) or len(attempts) >= config['weekly_attempt_limit']:
        result['deferred'] = 'certificate issuance cooldown/rate budget'
        return result
    # Validate before any certificate changes; reserve the budget before talking to the CA.
    runtime.nginx_check()
    runtime.backup()
    state.update(attempts=attempts + [now], retry_after=now + config['failure_retry_seconds'],
                 pending_reload=sorted(required), previous_names=sorted(existing))
    runtime.save(state)
    runtime.issue(required)
    if not required <= runtime.names():
        raise RuntimeError('Issued certificate lost required names; refusing nginx reload')
    state.update(pending_reload=sorted(required), retry_after=now + config['min_interval_seconds'])
    runtime.save(state)
    runtime.reload()
    state.pop('pending_reload')
    state.pop('previous_names', None)
    runtime.save(state)
    result['issued'] = True
    LOG.info('Certificate expanded, all %d previous names retained', len(existing))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/loyalup-tenant-ssl.json')
    parser.add_argument('--check', action='store_true', help='Print the plan without issuing or reloading')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    config = json.loads(Path(args.config).read_text())
    if not re.fullmatch(r'[a-z0-9.-]+', config['cert_name']):
        raise ValueError('Invalid certificate lineage name')
    state_dir = Path(config['state_dir'])
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    with (state_dir / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info('Another reconciliation is running')
            return
        state_path = state_dir / 'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        print(json.dumps(reconcile(config, state, Runtime(config), time.time(), args.check), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        LOG.error('%s', error)
        if isinstance(error, subprocess.CalledProcessError):
            LOG.error('Command output: %s', error.output)
        raise SystemExit(1)
