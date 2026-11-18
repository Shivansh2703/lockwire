/** A path as the person (and the model) should read it: inside the robot's folder, relative to that folder; outside it, `/…/name`
 * (`C:\…\name` on Windows): the folders are elided, the fact that it is absolute and outside is kept, so it can never be taken for a
 * file inside the folder. A bare home folder reads `~`. A `file:` address is read as the path it names (its scheme dropped).
 * The panel shows its lines through it, and main sends the model its text through it, so neither ever carries this machine's path.
 * `also`: other spellings of the same file (main passes its real path, symlinks resolved), stripped the same way before anything else.
 * `homes`: this machine's home folder (main passes os.homedir() and its real path). After the robot's folder, every occurrence of it
 * becomes `~` by plain literal replacement, case-insensitive, whatever stands before or after it: the account name cannot reach the
 * model through a form the patterns below miss. The patterns only tidy what is left. */
export const inFolder = (contract: string, also: string[] = [], homes: string[] = []) => {
  // macOS names one folder two ways (/tmp and /private/tmp, /var and /private/var): both sides are read in the short form before comparing.
  const short = (p: string) => p.replace(/^\/private(?=\/(?:tmp|var)\/)/, "");
  const folderOf = (p: string) => short(p.slice(0, Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\")) + 1));
  const folders = [...new Set([contract, ...also].map(folderOf))].filter((f) => f.length > 1).sort((a, b) => b.length - a.length);
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const inside = folders.length ? new RegExp(folders.map((f) => `(?:\\bfile:(?://)?)?(?:/private)?${esc(f)}`).join("|"), "g") : null;
  // The home folder as written, with its backslashes doubled (inside JSON text), and without a /private prefix; longest first.
  const hs = [...new Set(homes.filter((h) => h.length > 1).flatMap((h) => [h, h.replace(/\\/g, "\\\\"), short(h)]))].sort((a, b) => b.length - a.length);
  const homeAt = hs.length ? new RegExp(`(?:file:(?://(?:localhost)?)?)?(?:/private)?(?:${hs.map(esc).join("|")})`, "gi") : null;
  // Any other absolute path: after a `file:` scheme, a `word:`, a quote or a space; never inside a URL (`//host/...`), a unit (`N/m`), a
  // fraction, a date or `~/`. A Windows drive letter stands alone ("instance:" before an escaped newline is not one); its separators may be
  // doubled (a path inside JSON text) or forward slashes.
  const outside = /(?:\bfile:(?:\/\/)?(?=\/|[A-Za-z]:)|(?<![\w.~/]))(\/|[A-Za-z]:(?:\\{1,2}|\/))((?:[^\s"'`<>|,;()\\/]+(?:\\{1,2}|\/))+)([^\s"'`<>|,;()\\/]+)/g;
  const home = /^(?:Users|home)(?:\\{1,2}|\/)$/i;
  const literal = (text: string) => { const t = inside ? text.replace(inside, "") : text; return homeAt ? t.replace(homeAt, "~") : t; };
  return (text: string) => literal(text).replace(outside,(_m, root: string, dirs: string, name: string) => {
    if (home.test(dirs)) return "~"; // /Users/<name>, /home/<name>, C:\Users\<name>: never the account name
    const sep = root === "/" ? "/" : root.slice(2);
    return `${root === "/" ? "" : root.slice(0, 2)}${sep}…${sep}${name}`;
  });
};
