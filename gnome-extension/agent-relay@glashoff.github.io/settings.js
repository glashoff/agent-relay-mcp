// The extension's own settings, in ~/.config/agent-relay-mcp/extension.json
// (no GSettings schema needed, so installing stays a plain copy).

import GLib from 'gi://GLib';

const DEFAULTS = {
    // Notifications about activity (requests starting, approvals ending, ...),
    // besides those that need you (new requests, approvals about to end).
    notifyActivity: false,
};

function path() {
    return GLib.build_filenamev([GLib.get_user_config_dir(), 'agent-relay-mcp', 'extension.json']);
}

export function load() {
    try {
        const [ok, bytes] = GLib.file_get_contents(path());
        if (ok)
            return {...DEFAULTS, ...JSON.parse(new TextDecoder().decode(bytes))};
    } catch {
        // missing or unreadable: the defaults
    }
    return {...DEFAULTS};
}

export function save(settings) {
    try {
        GLib.mkdir_with_parents(GLib.path_get_dirname(path()), 0o700);
        GLib.file_set_contents(path(), JSON.stringify(settings, null, 2));
    } catch (error) {
        console.error(`Agent Relay: cannot save settings: ${error.message}`);
    }
}
