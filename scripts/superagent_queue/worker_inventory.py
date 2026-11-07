"""Resolve historical rows only through an explicit current native custody link."""


def completed_release(resource, current):
    liveness = (current.get('projection') or {}).get('liveness') or {}
    if not isinstance(liveness, dict):
        return False
    return (resource.get('ownershipState') == 'released' and resource.get('releaseState') == 'released'
            and isinstance(resource.get('releaseCompletedAt'), str) and bool(resource['releaseCompletedAt'])
            and current.get('terminalState') == 'released' and liveness.get('verdict') == 'exited'
            and liveness.get('source') == 'resource_release')


def transferred_resource(row, current):
    if not isinstance(current, dict):
        return None
    resource = current.get('resource')
    previous = row.get('projection') or {}
    projection = current.get('projection') or {}
    if not isinstance(previous, dict) or not isinstance(projection, dict):
        return None
    host = previous.get('host')
    workspace = previous.get('workspace')
    if (not isinstance(resource, dict) or not isinstance(host, dict) or not host.get('id')
            or projection.get('host') != host or current.get('runId') != row.get('runId')
            or resource.get('originDispatchId') != row.get('dispatchId')
            or resource.get('ownerDispatchId') != current.get('dispatchId')
            or not ((resource.get('ownershipState') == 'owned'
                     and resource.get('releaseState') in ('retained', 'not_requested'))
                    or completed_release(resource, current))
            or resource.get('terminalHandle') != row.get('agentTerminalHandle')
            or current.get('agentTerminalHandle') != resource.get('terminalHandle')
            or not isinstance(workspace, dict) or resource.get('worktreeId') != workspace.get('id')
            or any(not isinstance(resource.get(key), str) or not resource[key]
                   for key in ('id', 'endpointId', 'endpointIncarnation'))):
        return None
    return dict(resource)


def resource(row, rows):
    original = row.get('resource')
    if isinstance(original, dict):
        return dict(original)
    if (original is not None or row.get('dispatchStatus') != 'completed'
            or row.get('workerState') != 'succeeded' or row.get('terminalState') != 'retained'):
        return {}
    candidates = tuple(value for current in rows
                       if (value := transferred_resource(row, current)) is not None)
    return candidates[0] if len(candidates) == 1 else {}
