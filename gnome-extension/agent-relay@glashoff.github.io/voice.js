// The voice service (agent-relay-voice): its control socket and its systemd
// user unit.
//
// The service runs in a container without the session bus, so this is not
// D-Bus: JSON lines over the Unix socket
// $XDG_RUNTIME_DIR/agent-relay-voice/control.sock (docs/voice-plan.md,
// section 8). Nothing here can grant or approve relay access; the voice
// service has no such command, and the relay is not involved at all.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {ServiceClient} from './client.js';

export const VOICE_UNIT = 'agent-relay-voice.service';
const RETRY = 3;                    // seconds between connection attempts
const REPLY_TIMEOUT = 15;           // seconds to wait for a reply
const MAX_LINE = 1024 * 1024;       // longer lines from the service are dropped

export function socketPath() {
    return GLib.build_filenamev([GLib.get_user_runtime_dir(), 'agent-relay-voice', 'control.sock']);
}

// Talks to the voice service's control socket and reconnects whenever the
// socket appears again (the service started or restarted). onChange() is
// called whenever state, options, the connection or the listening flag may
// have changed.
//
// state: the service's state object, or null when it is not reachable.
// options: {stt, tts, claude_models} or null.
// listening: whether listening is on (true/false), or null when not known.
export class VoiceClient {
    constructor(onChange) {
        this._onChange = onChange;
        this.state = null;
        this.options = null;
        this.listening = null;
        this._connection = null;
        this._input = null;
        this._cancellable = null;
        this._pending = new Map();      // id: {resolve, reject, timeoutId}
        this._nextId = 1;
        this._writes = [];
        this._writing = false;
        this._connecting = false;
        this._retryId = 0;
        this._connect();
    }

    get connected() {
        return this._connection !== null;
    }

    destroy() {
        this._onChange = () => {};
        if (this._retryId)
            GLib.source_remove(this._retryId);
        this._retryId = 0;
        this._close();
    }

    // Try to connect now (e.g. when the menu opens) instead of waiting.
    poke() {
        if (this._connection)
            this.request('options').then(o => this._setOptions(o)).catch(() => {});
        else
            this._connect();
    }

    _scheduleRetry() {
        if (this._retryId)
            return;
        this._retryId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, RETRY, () => {
            this._retryId = 0;
            this._connect();
            return GLib.SOURCE_REMOVE;
        });
    }

    _connect() {
        if (this._connection || this._connecting)
            return;
        // Cheap check first: no socket file, no service.
        const path = socketPath();
        if (!GLib.file_test(path, GLib.FileTest.EXISTS)) {
            this._scheduleRetry();
            return;
        }
        this._connecting = true;
        const cancellable = new Gio.Cancellable();
        this._cancellable = cancellable;
        const client = new Gio.SocketClient();
        client.connect_async(Gio.UnixSocketAddress.new(path), cancellable, (c, res) => {
            this._connecting = false;
            let connection;
            try {
                connection = c.connect_finish(res);
            } catch {
                if (!cancellable.is_cancelled())
                    this._scheduleRetry();
                return;
            }
            if (cancellable.is_cancelled()) {
                connection.close(null);
                return;
            }
            this._connection = connection;
            this._input = new Gio.DataInputStream({
                base_stream: connection.get_input_stream(),
                close_base_stream: false,
            });
            this._readLine(cancellable);
            this._hello();
        });
    }

    async _hello() {
        try {
            const [state, options] = await Promise.all([
                this.request('state'), this.request('options')]);
            this._setState(state);
            this._setOptions(options);
        } catch (error) {
            console.warn(`Agent Relay: voice service did not answer: ${error.message}`);
        }
    }

    _close() {
        this._cancellable?.cancel();
        this._cancellable = null;
        this._connecting = false;
        try {
            this._connection?.close(null);
        } catch {
            // already closed
        }
        this._connection = null;
        this._input = null;
        this._writes = [];
        this._writing = false;
        for (const {reject, timeoutId} of this._pending.values()) {
            GLib.source_remove(timeoutId);
            reject(new Error('The voice service is not reachable.'));
        }
        this._pending.clear();
    }

    _lost() {
        const had = this._connection !== null || this.state !== null;
        this._close();
        this.state = null;
        this.options = null;
        this.listening = null;
        if (had)
            this._onChange();
        this._scheduleRetry();
    }

    _readLine(cancellable) {
        this._input.read_line_async(GLib.PRIORITY_DEFAULT, cancellable, (stream, res) => {
            let line;
            try {
                [line] = stream.read_line_finish_utf8(res);
            } catch {
                if (!cancellable.is_cancelled())
                    this._lost();
                return;
            }
            if (cancellable.is_cancelled())
                return;
            if (line === null) {            // the service closed the socket
                this._lost();
                return;
            }
            if (line.length <= MAX_LINE)
                this._onLine(line);
            this._readLine(cancellable);
        });
    }

    _onLine(line) {
        let message;
        try {
            message = JSON.parse(line);
        } catch {
            return;
        }
        if (!message || typeof message !== 'object')
            return;
        if ('id' in message && this._pending.has(message.id)) {
            const {resolve, reject, timeoutId} = this._pending.get(message.id);
            this._pending.delete(message.id);
            GLib.source_remove(timeoutId);
            if (message.ok)
                resolve(message.result ?? null);
            else
                reject(new Error(String(message.error ?? 'The voice service refused.')));
            return;
        }
        if (message.event === 'state' && message.state) {
            this._setState(message.state);
        } else if (message.event === 'exchange' && this.state) {
            this.state = {
                ...this.state,
                last_user: message.user ?? '',
                last_assistant: message.assistant ?? '',
            };
            this._onChange();
        }
    }

    _setState(state) {
        if (!state || typeof state !== 'object')
            return;
        this.state = state;
        // The state has no listening flag of its own; "paused" is listening
        // switched off, the phases that need the microphone mean it is on.
        // During thinking or speaking it keeps what was last known.
        if (typeof state.listening === 'boolean')
            this.listening = state.listening;
        else if (state.phase === 'paused')
            this.listening = false;
        else if (['listening', 'muted', 'hearing'].includes(state.phase))
            this.listening = true;
        this._onChange();
    }

    _setOptions(options) {
        if (!options || typeof options !== 'object')
            return;
        this.options = options;
        this._onChange();
    }

    // Sends a command; resolves to its result, rejects with a readable message.
    request(cmd, args = {}) {
        if (!this._connection)
            return Promise.reject(new Error('The voice service is not reachable.'));
        const id = this._nextId++;
        return new Promise((resolve, reject) => {
            const timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REPLY_TIMEOUT, () => {
                this._pending.delete(id);
                reject(new Error('The voice service did not answer.'));
                return GLib.SOURCE_REMOVE;
            });
            this._pending.set(id, {resolve, reject, timeoutId});
            this._write(`${JSON.stringify({...args, id, cmd})}\n`);
        });
    }

    // Writes one at a time: a stream allows only one pending operation.
    _write(text) {
        this._writes.push(new TextEncoder().encode(text));
        if (!this._writing)
            this._flush();
    }

    _flush() {
        const data = this._writes.shift();
        if (!data || !this._connection) {
            this._writing = false;
            return;
        }
        this._writing = true;
        const cancellable = this._cancellable;
        this._connection.get_output_stream().write_bytes_async(new GLib.Bytes(data),
            GLib.PRIORITY_DEFAULT, cancellable, (stream, res) => {
                let written;
                try {
                    written = stream.write_bytes_finish(res);
                } catch {
                    if (!cancellable?.is_cancelled())
                        this._lost();
                    return;
                }
                if (written < data.length)
                    this._writes.unshift(data.subarray(written));
                this._flush();
            });
    }

    // Updates the menu either way, so a switch that was refused flips back.
    async set(settings) {
        try {
            const result = await this.request('set', settings);
            if (typeof settings.listening === 'boolean')
                this.listening = settings.listening;
            return result;
        } finally {
            this._onChange();
        }
    }
}

// The service's systemd user unit. Its state comes from systemd on the
// session bus; starting and stopping goes through systemctl, which waits for
// the job and says in words what went wrong.
export class VoiceUnit {
    constructor() {
        this._systemd = new ServiceClient(VOICE_UNIT);
        this.load = 'unavailable';     // 'loaded', 'not-found', 'unavailable', ...
        this.active = 'unavailable';   // 'active', 'inactive', 'activating', 'failed', ...
    }

    get installed() {
        return this.load === 'loaded';
    }

    get on() {
        return this.active === 'active' || this.active === 'activating' ||
            this.active === 'reloading';
    }

    get changing() {
        return this.active === 'activating' || this.active === 'deactivating' ||
            this.active === 'reloading';
    }

    // Resolves to true when something changed.
    async refresh() {
        const {load, active} = await this._systemd.unitState();
        const changed = load !== this.load || active !== this.active;
        this.load = load;
        this.active = active;
        return changed;
    }

    // verb: 'start' or 'stop'
    control(verb) {
        return new Promise((resolve, reject) => {
            let proc;
            try {
                proc = Gio.Subprocess.new(['systemctl', '--user', verb, VOICE_UNIT],
                    Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE);
            } catch (error) {
                reject(new Error(error.message));
                return;
            }
            proc.communicate_utf8_async(null, null, (p, res) => {
                try {
                    const [, , stderr] = p.communicate_utf8_finish(res);
                    if (p.get_successful())
                        resolve();
                    else
                        reject(new Error(stderr?.trim() || `systemctl ${verb} failed.`));
                } catch (error) {
                    reject(new Error(error.message));
                }
            });
        });
    }
}
