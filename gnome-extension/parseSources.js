// Pure parsing logic, deliberately free of any gi://* or resource:///*
// imports so it can be unit-tested with plain Node (GNOME Shell's own
// modules only exist inside a running shell process).

export function parseSourceFile(text) {
    return text.trim().split('\n')
        .filter(Boolean)
        .map(line => {
            const [path, date] = line.split('\t');
            return {path: path ?? line, date: date ?? ''};
        });
}
