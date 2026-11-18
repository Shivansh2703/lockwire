# Reading a datasheet
Taking a number from a maker's datasheet into the robot file, with its source.

## The idea
- A declared value carries a source: a file beside the robot file, a URL or a cited title (`source_portability`).
- A catalog part is "cited" when every number comes from the maker's own datasheet and names the URL it was read from. Anything else (a retail listing, an assumed placeholder, a figure with no URL) is "unverified". The label is worked out from the numbers, never typed in.
- Lockwire compares the model with the datasheets it holds: a recognised robot's numbers with its vendor datasheet (`datasheet_agreement`), a joint's limit span with its servo's rated travel (`datasheet_limit`), and a joint's effort limit with the servo's stall torque (`servo_stall`).

## Tools, in order
1. `catalog-parts`: is the part already in the catalog? Its numbers come with their source lines.
2. If you have a file-reading tool and the datasheet is in the robot's folder, read the page and quote the line that holds the number. Otherwise ask the person for the figure, its unit and where it is printed.
3. `declare-form`: which declared value the number belongs to.
4. `what-if` with the value and its source, then `declare` with the same source. The person presses Apply.

## Traps
- Stall torque and continuous torque are different rows. `explain-finding` names the one a check compares; say which row you took.
- A rating holds at one voltage. Give the voltage printed beside the figure: a servo on a lower supply gives less.
- Units: kg*cm and N*m, g and kg. Do not convert by hand. Quote a conversion the engine printed (a catalog source line shows one), or give the value with the datasheet's own unit and say so.
- A retail listing is not a datasheet. A number from one stays unverified.
- If you cannot see the number on a page or in a tool result, say it is not on record. Never fill a gap from memory.
