"""Runtime Oracle ZDM release and command-capability detection."""
import functools
import re

SUPPORTED_RELEASES = {(21, 4), (21, 5), (26, 1)}


def parse_build(output):
    """Parse the ZDM version without confusing it with the CPAT version."""
    full = re.search(r'(?im)^\s*full version\s*:\s*["\']?([0-9]+(?:\.[0-9]+)+)', output or '')
    patch = re.search(r'(?im)^\s*patch version\s*:\s*["\']?([^\r\n"\']*)', output or '')
    value = full.group(1) if full else ''
    numbers = tuple(int(part) for part in value.split('.')[:2]) if value else ()
    release = '.'.join(map(str, numbers)) if numbers else 'unknown'
    return {
        'full_version': value,
        'patch_version': patch.group(1).strip() if patch else '',
        'release': release,
        'release_tuple': numbers,
        'supported': numbers in SUPPORTED_RELEASES,
    }


def documentation(release):
    if release not in {'21.4', '21.5', '26.1'}:
        release = '26.1'
    return f'https://docs.oracle.com/en/database/oracle/zero-downtime-migration/{release}/zdmug/'


def _help(args):
    from . import adapter
    try:
        return True, adapter.run(args, timeout=20)
    except (RuntimeError, TimeoutError, OSError) as exc:
        return False, str(exc)


def _switches(output):
    return set(re.findall(r'(?<![A-Za-z0-9_])-([a-z][a-z0-9]*)\b', output or '', re.I))


@functools.lru_cache(maxsize=1)
def current():
    """Probe the installed patch. Observed CLI behavior wins over docs."""
    from . import adapter
    build = adapter.run(['-build'], timeout=20)
    release = parse_build(build)
    commands = {}
    for name, args in {
        'migrate_database': ['migrate', 'database', '-help'],
        'resume_job': ['resume', 'job', '-help'],
        'modify_job': ['modify', 'job', '-help'],
    }.items():
        ok, output = _help(args)
        unsupported = 'not supported for object "job"' in output.lower()
        commands[name] = {
            'supported': bool(ok and not unsupported),
            'switches': sorted(_switches(output)) if ok else [],
        }
    warnings = []
    if not release['supported']:
        warnings.append('This installer supports ZDM 21.4, 21.5, and 26.1; detected ' + release['release'] + '.')
    if not commands['modify_job']['supported']:
        warnings.append('The installed ZDM patch does not support modify job; that action is disabled.')
    capabilities = {
        'migrate_database': commands['migrate_database']['supported'],
        'migrate_switches': commands['migrate_database']['switches'],
        'resume_job': commands['resume_job']['supported'],
        'resume_switches': commands['resume_job']['switches'],
        'modify_job': commands['modify_job']['supported'],
        'modify_switches': commands['modify_job']['switches'],
    }
    return {
        **release,
        'build_output': build,
        'documentation': documentation(release['release']),
        'capabilities': capabilities,
        'warnings': warnings,
    }


def require_supported():
    detected = current()
    if not detected['supported']:
        raise RuntimeError(detected['warnings'][0])
    return detected


def supports_switch(command, name):
    detected = require_supported()
    return name.lower() in set(detected['capabilities'].get(command + '_switches', []))


def reset():
    current.cache_clear()
