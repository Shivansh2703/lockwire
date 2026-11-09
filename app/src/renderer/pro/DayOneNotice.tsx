// An empty ledger means nothing has been measured yet, never that
// anything is wrong. Only the document's own `next_step` sentence, verbatim -- no chart
// furniture, no invented threshold or verdict. The "Add a baseline" action needs an App
// callback to Docs and is not wired here.
export function DayOneNotice({ nextStep }: { nextStep: string | null }) {
  if (!nextStep) return null;
  return (
    <p className="day-one-notice">{nextStep}</p>
  );
}
