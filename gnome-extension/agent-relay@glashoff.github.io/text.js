// Small text helpers. Everything shown comes from the relay and, for
// requests, from the chat: it is only ever set as plain text.

export function oneLine(text, max = 300) {
    const flat = String(text ?? '').replace(/\s+/g, ' ').trim();
    return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

export function duration(seconds) {
    const minutes = Math.max(0, Math.floor(seconds / 60));
    if (minutes >= 60)
        return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
    return `${minutes} min`;
}

export function where(kind) {
    return kind === 'host' ? 'on the host' : 'in the dev container';
}

export function targetName(target) {
    return target === '*' ? 'all projects' : target;
}
