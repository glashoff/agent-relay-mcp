// The relay's control interface on the session bus, and its systemd unit.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const NAME = 'io.github.glashoff.AgentRelay1';
const PATH = '/io/github/glashoff/AgentRelay1';
export const UNIT = 'agent-relay-mcp.service';

const IFACE = `<node>
  <interface name="${NAME}">
    <method name="GetState"><arg direction="out" type="s"/></method>
    <method name="Grant"><arg direction="in" type="s"/><arg direction="out" type="s"/></method>
    <method name="Approve"><arg direction="in" type="s"/><arg direction="out" type="s"/></method>
    <method name="Deny"><arg direction="in" type="s"/></method>
    <method name="Revoke">
      <arg direction="in" type="s"/><arg direction="in" type="b"/><arg direction="out" type="s"/>
    </method>
    <method name="RevokeAll"><arg direction="in" type="b"/><arg direction="out" type="u"/></method>
    <method name="CancelSwitch"><arg direction="in" type="s"/></method>
    <method name="StopProject">
      <arg direction="in" type="s"/><arg direction="in" type="b"/><arg direction="out" type="s"/>
    </method>
    <method name="SetKeepAwake">
      <arg direction="in" type="s"/><arg direction="in" type="b"/><arg direction="out" type="s"/>
    </method>
    <signal name="Event"><arg type="s"/></signal>
  </interface>
</node>`;

const RelayProxy = Gio.DBusProxy.makeProxyWrapper(IFACE);

function message(error) {
    if (error instanceof GLib.Error && Gio.DBusError.is_remote_error(error))
        Gio.DBusError.strip_remote_error(error);
    return error.message;
}

// Talks to the relay. onChange() is called whenever its state may have
// changed (or it came or went), onEvent(event) for each event it sends.
export class RelayClient {
    constructor(onChange, onEvent) {
        this._onChange = onChange;
        this._onEvent = onEvent;
        this.state = null;          // null: the relay is not reachable
        this._proxy = null;
        this._signalId = 0;
        this._watchId = Gio.bus_watch_name(Gio.BusType.SESSION, NAME,
            Gio.BusNameWatcherFlags.NONE,
            () => this.refresh(),
            () => {
                this.state = null;
                this._onChange();
            });
        this._proxy = new RelayProxy(Gio.DBus.session, NAME, PATH, (proxy, error) => {
            if (error) {
                console.error(`Agent Relay: no proxy: ${error.message}`);
                return;
            }
            this._signalId = proxy.connectSignal('Event', (_p, _sender, [json]) => {
                let event;
                try {
                    event = JSON.parse(json);
                } catch {
                    return;
                }
                this._onEvent(event);
                this.refresh();
            });
            this.refresh();
        }, null, Gio.DBusProxyFlags.DO_NOT_AUTO_START);
    }

    destroy() {
        if (this._watchId)
            Gio.bus_unwatch_name(this._watchId);
        this._watchId = 0;
        if (this._proxy && this._signalId)
            this._proxy.disconnectSignal(this._signalId);
        this._proxy = null;
        this._onChange = () => {};
        this._onEvent = () => {};
    }

    async refresh() {
        if (!this._proxy)
            return;
        try {
            const [json] = await this._proxy.GetStateAsync();
            this.state = JSON.parse(json);
        } catch {
            this.state = null;
        }
        this._onChange();
    }

    // Calls a method; resolves to its result, rejects with a readable message.
    async call(method, ...args) {
        if (!this._proxy)
            throw new Error('The relay is not reachable.');
        try {
            const out = await this._proxy[`${method}Async`](...args);
            return out.length ? out[0] : null;
        } catch (error) {
            throw new Error(message(error));
        } finally {
            this.refresh();
        }
    }

    async grant(options) {
        return JSON.parse(await this.call('Grant', JSON.stringify(options)));
    }

    async approve(options) {
        return JSON.parse(await this.call('Approve', JSON.stringify(options)));
    }
}

function busCall(service, path, iface, method, args, replyType) {
    return new Promise((resolve, reject) => {
        Gio.DBus.session.call(service, path, iface, method, args,
            replyType ? new GLib.VariantType(replyType) : null,
            Gio.DBusCallFlags.NO_AUTO_START, 5000, null, (conn, res) => {
                try {
                    resolve(conn.call_finish(res));
                } catch (error) {
                    reject(new Error(message(error)));
                }
            });
    });
}

// The relay's systemd user unit: its state, and starting, stopping and
// restarting it. On a bus without systemd (a test session) it is "unavailable".
export class ServiceClient {
    async activeState() {
        try {
            const [path] = (await busCall('org.freedesktop.systemd1', '/org/freedesktop/systemd1',
                'org.freedesktop.systemd1.Manager', 'LoadUnit',
                new GLib.Variant('(s)', [UNIT]), '(o)')).deepUnpack();
            const [value] = (await busCall('org.freedesktop.systemd1', path,
                'org.freedesktop.DBus.Properties', 'Get',
                new GLib.Variant('(ss)', ['org.freedesktop.systemd1.Unit', 'ActiveState']),
                '(v)')).deepUnpack();
            return value.deepUnpack();
        } catch {
            return 'unavailable';
        }
    }

    // verb: 'Start', 'Stop' or 'Restart'
    async control(verb) {
        await busCall('org.freedesktop.systemd1', '/org/freedesktop/systemd1',
            'org.freedesktop.systemd1.Manager', `${verb}Unit`,
            new GLib.Variant('(ss)', [UNIT, 'replace']), '(o)');
    }
}
