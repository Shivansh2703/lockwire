# A part from a datasheet
Adding a new part from a datasheet PDF in the robot's folder, every number with its page and the line that shows it.

## The idea
- The person drops the maker's PDF into the robot's folder and asks for the part. The card adds one device whose source names the PDF and, for each number, its page and the line from that page.
- Lockwire checks that each quote shows its number on its own, followed by its unit, in the datasheet's own unit (12 kg in "Weight 12 kg"; not 12 g, not 2 kg). It does not read the PDF for you and does not check the page or the quote against it: the numbers, pages and quotes are yours, so copy them exactly.
- Only the mass (a number named "mass" or "weight") becomes a declared value on the device. The other numbers are recorded in its source with their page and line.
- The card shows the engine's check before and after. Nothing is written until the person applies it.

## Tools, in order
1. `list-files` with a glob such as `*.pdf`: find the datasheet. Use the path exactly as listed.
2. `read-file` on that PDF, in the same turn as the proposal. A PDF read in an earlier turn is no longer in front of you, and `propose-part` refuses it.
3. `propose-part` with the PDF's path, a new device id, the family, the model, the owner, and each number with its name, value, unit, page and the line that shows it.
4. If it is refused, the reply says which number or which rule. Fix that one and call it again.

## Traps
- The page is the PDF's page number, counted from the first page of the file, not a number printed in a footer.
- A quote must show the number alone, then its unit, in the form you give: 1.5 kg for 1.5 kg, not 1,5 or 1.50. Give the value in the datasheet's own unit; do not convert it.
- Ratings hold at one voltage. Quote the line with the voltage beside the figure, and say which voltage it is.
- Stall torque and rated torque are different rows. Say which one you took.
- If a number is not printed in the PDF, leave it out and say it is not on record. Never fill a gap from memory or from another part.
