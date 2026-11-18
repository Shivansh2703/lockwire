// A stored witness as a person reads it, the words `lockwire` prints (edit.py `witness_words`):
// a name as is, a level code (w0..w6, human, live) in words, nothing named as "none named".
const LEVEL_WORDS: Record<string, string> = {
  w0: "none named (declared, not measured)",
  w2: "the model file",
  human: "a person, not named",
  live: "live reading",
};

export function witnessWords(witness: string | null | undefined): string {
  if (!witness?.trim()) return "no name on record";
  return /^(w[0-6]|human|live)$/.test(witness) ? LEVEL_WORDS[witness] ?? "none named" : witness;
}
