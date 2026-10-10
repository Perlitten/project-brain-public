// Index revisions arrive as a git SHA or as "<kind>:<hash>" for sources that
// aren't a git checkout (snapshot:, manifest:). Show seven hash characters
// either way — never the prefix, which read as "manifes" before.
export const shortRevision = (rev: string | null | undefined) => (rev ? rev.replace(/^[a-z]+:/i, "").slice(0, 7) : "");
