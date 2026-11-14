# Webots robot masses against vendor figures

## Summary

We summed the `Physics` mass of every robot PROTO in `projects/robots` of cyberbotics/webots (commit b3f283949798b8f26a3dcc4df931f71f5148bc93, a Webots R2025b development state) and compared it with the weight the robot's own maker publishes. 73 robot models were looked at. 10 agree within 5 %, 14 differ by more than 5 % and need a scope check before anyone calls them a bug (CANDIDATE), 1 is a clear mismatch with a one-line fix drafted but not yet submitted upstream (CATCH: the Bitcraze Crazyflie, 50 g in the model against 31.3 g from the vendor), and 48 could not be compared (no vendor weight found, or the model does not state a mass). Franka Emika Panda is counted as agreeing on its default configuration (hand included, -2.0 %); the arm alone would be -6.2 %. Nothing here is a claim that a model is wrong: a vendor weight and a simulation mass are often measured with different things attached. The table says which.

## Method

Model mass. A small standard-library Python script reads each robot PROTO and adds up every `Physics` node.
- `density -1` with `mass > 0`: the mass is given and is added.
- `density > 0` (the Webots default is 1000): the mass comes from the geometry. These solids are counted but NOT added, so a model with such solids is a lower bound and is marked "from density" or "partly from density". Robots whose mass is entirely density-derived are NOT COMPARED.
- A `Propeller` counts its slow helix only (the fast helix is the spinning alternative). A `USE` of a named node counts that node again. Nested PROTOs are followed and counted once per instance.
- Templated PROTOs (`%< ... >%`): the script drops the template statements, keeps both branches of every `if`, and runs loops once. Such files are flagged `templated` and the sum is approximate; the default configuration was checked by hand for each one in the table (for example, KUKA youBot has a second-arm branch that is absent by default, which is why its row shows the default-configuration figure in its note). A `mass IS x` reference takes the default of field `x` from the PROTO header. A `staticBase` field that removes a base mass at its default is named in the row note (UR3e, UR5e, UR10e).

Vendor figure. The standard is the vendor's own technical source only: a datasheet, manual, specification table or technical page of the robot's maker. Retailer pages, forum posts and third-party reviews are not used. Where the vendor page was only reachable as an archived or mirrored copy of the vendor's own file, the table says so. The exact quoted scope words (with cable, with battery, without sensors, and so on) are in the table, and the model-side scope was recorded in the working data, which is not published with this report. Retrieval date for every row is 2026-11-05 unless the row says otherwise.

Verdicts. The tolerance is 5 %.
- AGREES: the absolute difference is 5 % or less.
- CANDIDATE: more than 5 % apart; a scope or revision difference could explain it, and the section below says what to check first.
- CATCH: more than 5 % apart and the vendor scope has been matched as far as the vendor's documents allow, so scope does not explain it.
- NOT COMPARED: no usable vendor weight, or the model has no stated mass.

Delta is (model - vendor) / vendor.

## Full table

| Robot | Model kg | Vendor kg | Vendor scope words | Delta % | Verdict | Source |
|---|---|---|---|---|---|---|
| UR3e | 10.7985 | 11.2 | "Weight including cable 11,2 kg" (6 m cable) | -3.6 | AGREES | https://www.universal-robots.com/media/1807464/ur3e_e-series_datasheets_web.pdf |
| UR5e | 21.7 | 20.6 | "Weight including cable 20,6 kg" (6 m cable) | 5.3 | CANDIDATE | https://www.universal-robots.com/media/1807465/ur5e_e-series_datasheets_web.pdf |
| UR10e | 33.104 | 33.5 | "Weight including cable 33,5 kg" (6 m cable) | -1.2 | AGREES | https://www.universal-robots.com/media/1807466/ur10e_e-series_datasheets_web.pdf |
| Franka Emika Panda | 17.4519 | 17.8 | "Weight ~ 17.8 kg" (robot section; the "Moving mass ~ 12.8 kg" and controller "Weight ~ 7 kg" are separate lines). Earlier May 2018 datasheet (reseller copy) said "weight ~ 18 kg" | -2.0 | AGREES | https://s3-eu-central-1.amazonaws.com/franka-de-uploads/uploads/Datasheet-EN.pdf (Wayback 2019-10-20 copy of Franka Emika own file) |
| Epson SCARA T6 | 22.001 | 22 | "Weight: 22 kg: 48.5 lb" ; sensotek mirror of Epson sheet: "Weight (cables not included) 22 kg" | 0.0 | AGREES | https://epson.com/For-Work/Robots/SCARA/T6-B-All-in-One-SCARA-Robot/p/RT6B-602SS |
| Niryo Ned | 1.4622 | 6.5 | "Weight 6,5 kg" (no scope words in the table; Ned technical specifications page, Wayback 2023 copy of docs.niryo.com) | -77.5 | CANDIDATE | https://web.archive.org/web/2023/https://docs.niryo.com/product/ned/source/hardware/technical_specifications.html |
| Irb4600-40 | 0.0 | | | | NOT COMPARED | |
| F&P Robotics P-Rob 3 | 20.9593 | | | | NOT COMPARED | |
| DJI Mavic 2 Pro | 0.9069 | 0.907 | "Aircraft Takeoff Weight Mavic 2 Pro: 907 g" | -0.0 | AGREES | https://www.dji.com/mavic-2/info |
| Bitcraze Crazyflie | 0.05 | 0.0313 | "Takeoff weight: 29g" (Crazyflie 2.1) plus "Weight: 2.3g" (Multi-ranger deck); datasheet also "Max recommended payload weight: 15 g" | 59.7 | CATCH | https://www.bitcraze.io/documentation/hardware/crazyflie_2_1/crazyflie_2_1-datasheet.pdf ; https://www.bitcraze.io/products/multi-ranger-deck/ |
| Boston Dynamics Spot | 27.2257 | 32.5 | "Net Weight 32.5 kg (71.7 lbs)"; battery listed separately "Weight 4.2 kg" | -16.2 | CANDIDATE | https://bostondynamics.com/wp-content/uploads/2020/05/spot-spec-sheet.pdf |
| Boston Dynamics Atlas (DRC) | 89.0291 | | | | NOT COMPARED | |
| Adept Pioneer 3-AT | 12.0 | 14 | "Weight (kg) 14" (3-AT column, Appendix C Specifications, Pioneer 3 operations manual; the 3-DX text says "Weighing only 9 kg (20 pounds with one battery)") | -14.3 | CANDIDATE | http://vigir.missouri.edu/~gdesouza/Research/MobileRobotics/Software/P3OpMan5.pdf |
| Adept Pioneer 3-DX | 6.73 | 9 | "Weighing only 9 kg (20 pounds with one battery)" |  | NOT COMPARED | http://vigir.missouri.edu/~gdesouza/Research/MobileRobotics/Software/P3OpMan5.pdf |
| Husarion ROSbot | 2.45 | 2.45 | "Weight 2,84 kg / 100 oz (with camera and LiDAR), 2,45 kg / 86 oz (without camera and LiDAR)" | 0.0 | AGREES | https://husarion.com/manuals/rosbot/ |
| Husarion ROSbot XL | 3.704 | 5.38 | "Weight * 5380g" with footnote "*With mecanum wheels, RPi5 SBC and no sensors." | -31.2 | CANDIDATE | https://husarion.com/manuals/rosbot-xl/ |
| ROBOTIS OP3 | 3.1475 | 3.5 | "About 3.5kg (without skin cover)" | -10.1 | CANDIDATE | https://emanual.robotis.com/docs/en/platform/op3/introduction/ |
| ROBOTIS DARwIn-OP | 2.9253 | 2.9 | "Weight: 2.9kg (6.39lb)" (Mechanical specs) | 0.9 | AGREES | https://emanual.robotis.com/docs/en/platform/op/references/ |
| ROBOTIS DARwIn-OP (Hinge2 variant) | 3.1077 | 2.9 | "Weight: 2.9kg (6.39lb)" (Mechanical specs of DARwIn-OP) | 7.2 | CANDIDATE | https://emanual.robotis.com/docs/en/platform/op/references/ |
| SoftBank/Aldebaran NAO | 5.1476 | 5.4 | "Weight: 5.4kg" (NAO H25 Construction) | -4.7 | AGREES | http://doc.aldebaran.com/2-4/family/nao_h25/dimensions_h25.html |
| KUKA youBot | 37.168 | 25.3 | base "Overall Weight : ~20 kg"; arm "Weight : 5.3 kg" (vendor wiki). Same vendor Kinematics page: base frame Mass 19.8 kg excluding wheels and plate, wheels 1.40 kg each, plate 2.397 kg | 46.9 | CANDIDATE | http://www.youbot-store.com/wiki/index.php/YouBot_Detailed_Specifications (Wayback 2016-11-02); http://www.youbot-store.com/developers/kuka-youbot-kinematics-dynamics-and-3d-model (Wayback 2019-03-20) |
| MiR100 | 77.273 | 62.5 | "Weight (without load) 62.5 kg" (MiR100 data sheet, 05 2016) | 23.6 | CANDIDATE | https://howtorobot.com/sites/default/files/2021-12/mir100-datasheet_%282%291.pdf |
| GCtronic Elisa-3 | 0.039 | 0.039 | "Size, weight 50 mm diameter, 30 mm height, 39 g" |  | NOT COMPARED | http://www.gctronic.com/doc/index.php/Elisa-3 |
| GCtronic e-puck | 0.16 | 0.15 | "Size, weight 70 mm diameter, 55 mm height, 150 g" (e-puck version 1.3 column; version 2 column: 130 g) | 6.7 | CANDIDATE | http://www.gctronic.com/doc/index.php/e-puck2 |
| ROBOTIS TurtleBot3 Burger | 0.8827 | 1.0 | "Weight (+ SBC + Battery + Sensors) 1kg" (Burger column) |  | NOT COMPARED | https://emanual.robotis.com/docs/en/platform/turtlebot3/features/ |
| Mobsya Thymio II | 0.248 | | | | NOT COMPARED | |
| Clearpath Moose | 1590.0 | | | | NOT COMPARED | |
| A4 Portal | 0.0 | | | | NOT COMPARED | |
| Adept Pioneer 2 | 0.0 | | | | NOT COMPARED | |
| Clearpath Heron | 0.0 | | | | NOT COMPARED | |
| K-Team Hemisson | 0.0 | | | | NOT COMPARED | |
| K-Team Khepera I | 0.0 | | | | NOT COMPARED | |
| K-Team Khepera II | 0.0 | | | | NOT COMPARED | |
| K-Team Koala | 0.0 | | | | NOT COMPARED | |
| Kinematics Tinkerbots (base) | 0.0 | | | | NOT COMPARED | |
| Picaxe Microbot | 0.0 | | | | NOT COMPARED | |
| Sony QRIO | 0.0 | | | | NOT COMPARED | |
| Unimation Puma 560 | 0.0 | | | | NOT COMPARED | |
| Distributor (NAO sample object) | 0.0 | | | | NOT COMPARED | |
| Surveyor SRV-1 | 0.324 | | | | NOT COMPARED | |
| Parallax Boe-Bot | 0.1 | | | | NOT COMPARED | |
| NVIDIA JetBot | 0.5641 | | | | NOT COMPARED | |
| REC Fabtino | 30.0 | | | | NOT COMPARED | |
| Micromagic Scout | 1.01 | | | | NOT COMPARED | |
| EPFL GhostDog | 7.0992 | | | | NOT COMPARED | |
| PAL Robotics TIAGo | 72.6128 | 72 | "Dimensions Height 110 - 145 cm Weight 72 Kg Base footprint Ø 54 cm ... Arm Payload 2 Kg" (TIAGo column; PAL OS hardware overview) | 0.9 | AGREES | https://docs.pal-robotics.com/25.01/hardware/tiago/hardware-overview.html |
| PAL Robotics TIAGo Base | 32.732 | 40 | "The total weight of TIAGo Base is about 40 kg, including batteries." ; battery table "Weight 7.5 kg" | -18.2 | CANDIDATE | https://docs.pal-robotics.com/tiago-base/handbook.html |
| PAL Robotics TIAGo Lite | 54.961 | 60 | "Weight 60 Kg" (TIAGo Lite tab, first tab of the specifications overview) | -8.4 | CANDIDATE | https://docs.pal-robotics.com/25.01/hardware/tiago/hardware-overview.html |
| PAL Robotics TIAGo++ | 90.2647 | | | | NOT COMPARED | |
| Festo Robotino 3 | 23.0 | 20 | "Total weight (curb weight) 20 kg ... Total weight (including 4 battery packs) 22,8 kg" | 15.0 | CANDIDATE | https://ip.festo-didactic.com/InfoPortal/Robotino/Overview/EN/index.html |
| Robotnik Summit-XL Steel | 151.0 | 105 | "Weight 105 kg ... Payload 130 kg / 250 kg" (datasheet 210628, no scope words for the weight) | 43.8 | CANDIDATE | https://robotnik.eu/wp-content/uploads/2021/06/Robotnik-SUMMIT-XL-STEEL-Datasheet-210628-EN.pdf |
| Fujitsu HOAP-2 | 7.1599 | | | | NOT COMPARED | |
| iRobot Create | 2.4 | | | | NOT COMPARED | |
| K-Team Khepera III | 0.71 | | | | NOT COMPARED | |
| K-Team Khepera IV | 0.606 | | | | NOT COMPARED | |
| Kondo KHR-2HV | 1.624 | | | | NOT COMPARED | |
| Kondo KHR-3HV | 1.7 | | | | NOT COMPARED | |
| LEGO Mindstorms rover | 0.202 | | | | NOT COMPARED | |
| Micromagic Mantis | 236.1 | | | | NOT COMPARED | |
| NASA Sojourner | 11.0 | | | | NOT COMPARED | |
| Neuronics Katana HD6M180 | 3.83 | | | | NOT COMPARED | |
| Neuronics Katana HD6M90 | 3.83 | | | | NOT COMPARED | |
| Neuronics Katana HD6MS180 | 3.83 | | | | NOT COMPARED | |
| Neuronics Katana HD6MS90 | 3.83 | | | | NOT COMPARED | |
| Nex Robotics Fire Bird VI | 2.41 | | | | NOT COMPARED | |
| Robotis Bioloid dog | 1.76 | | | | NOT COMPARED | |
| Sony AIBO ERS-7 | 1.628 | 1.6 | "Weight Approx. 3 lb. 8 oz. (1.6 kg) (including battery pack and 'Memory Stick' media)" (Sony ERS-7 user manual, Specifications page) | 1.7 | AGREES | https://www.manualsdir.com/manuals/439516/sony-ers-7.html?page=110 |
| Sphero BB-8 | 26.0 | | | | NOT COMPARED | |
| BlueBotics Shrimp | 6.4089 | | | | NOT COMPARED | |
| Willow Garage PR2 | 277.6272 | | | | NOT COMPARED | |
| EPFL Salamander | 1.8482 | | | | NOT COMPARED | |
| EPFL Yamor | 0.186 | | | | NOT COMPARED | |
| EPFL Blimp | 0.184 | | | | NOT COMPARED | |

## Candidates: what to check before calling any of them a bug

- UR5e (+5.3 %). The model is heavier than a vendor figure that already includes a 6 m cable, so cable scope does not explain it. Check the link masses against Universal Robots' own CAD or URDF data, and note that at the default `staticBase TRUE` the model sums to 17.7 kg (-14 %), so which configuration the vendor weight describes matters.
- Niryo Ned (-77.5 %). The model is 1.46 kg against 6.5 kg. A massless static base link explains part of it and not likely all. Check the link masses in Niryo's own ROS URDF before saying anything.
- Boston Dynamics Spot (-16.2 %). If the 32.5 kg "net weight" includes the 4.2 kg battery, the model without battery (28.3 kg) is -3.8 %. The sheet does not say; check Boston Dynamics' documentation for what net weight includes.
- Adept Pioneer 3-AT (-14.3 %). The 14 kg is from a university-hosted copy of the manufacturer's manual and does not state a battery scope. Check the figure with and without batteries.
- Husarion ROSbot XL (-31.2 %). The vendor figure is for the current RPi5 build with mecanum wheels. The model may predate that hardware revision. Check the revision the PROTO was built from.
- ROBOTIS OP3 (-10.1 %). The model sum is a lower bound (one density-derived solid not added) and the vendor figure is "About 3.5 kg". Likely within tolerance once that solid is counted; check its derived mass.
- ROBOTIS DARwIn-OP Hinge2 variant (+7.2 %). The variant is a Cyberbotics modification with extra joints, compared with the base robot's 2.9 kg. Not a vendor product, so probably not a defect.
- KUKA youBot (+46.9 % as summed over all template branches, about +21 % in the default one-arm configuration, 30.7 kg against 25.3 kg). The vendor's own pages disagree with each other (their 3D-model page implies about 33 kg), and the 25.3 kg is a sum of two vendor lines. Check which youBot figure is authoritative and whether the model's wheels and plate are counted twice.
- MiR100 (+23.6 %). Other MiR brochures list 65 and 70 kg; against 70 kg the delta is +10.4 %. Check which data sheet revision the model followed.
- GCtronic e-puck (+6.7 %). The vendor figure is a rounded 150 g (the version 2 column says 130 g). Likely rounding; check which version the model is.
- PAL Robotics TIAGo Base (-18.2 %). The vendor says "including batteries"; 40 kg minus the 7.5 kg battery line is 32.5 kg, which is -0.7 % from the model. Check whether the model omits batteries on purpose.
- PAL Robotics TIAGo Lite (-8.4 %). The column of the vendor table was read from page order, so first confirm the tab. Then check the same battery scope as for TIAGo Base.
- Festo Robotino 3 (+15 %). The model body equals the 20 kg curb weight and the wheels are added on top, which looks like a double count of the wheels; against the 22.8 kg with battery packs it is +0.9 %. Note that the Festo page is headed Robotino 4.
- Robotnik Summit-XL Steel (+43.8 %). The strongest unresolved row apart from the catch. Payload is a separate line, so 105 kg is the unloaded robot, and a battery or option variant cannot add 46 kg. No scope words in the sheet. Ask Robotnik which configuration 105 kg describes.

## The catch: Bitcraze Crazyflie

`projects/robots/bitcraze/crazyflie/protos/Crazyflie.proto` line 222 has `mass 0.05`. Bitcraze's Crazyflie 2.1 datasheet states "Takeoff weight: 29g", its Multi-ranger deck page states "Weight: 2.3g", and the model includes the four Multi-ranger range sensors, giving 31.3 g (model +60 %). The datasheet also gives "Max recommended payload weight: 15 g"; taking the takeoff weight plus the full payload (44 g) the model is still +12 %. The controller's altitude feedforward (48 rad/s, with four propellers at `thrustConstants 4e-05`) carries about 37.6 g, so the controller was not set for 50 g either, and the altitude controller has an integral term. The fix is a one-line change to `mass 0.0313`. The fix is drafted as a patch. It has not been submitted to cyberbotics/webots as a pull request. Not verified: we did not run the Crazyflie worlds with the new mass, and a lower mass raises angular acceleration under the same attitude gains.

## Limits

48 of 73 were not compared:
- 19: no vendor weight sheet found (including Atlas DRC, PR2, Khepera III and IV, Kondo KHR, Katana, Mantis, BB-8 and others).
- 12: the model's mass comes (entirely or partly) from density, so no model mass can be stated (7 fully derived, 5 partly with a lower bound only).
- 6: the model has no mass at all.
- 3: partly density-derived, so only a lower bound exists (Micromagic Scout, EPFL GhostDog, TurtleBot3 Burger).
- 3: research prototypes (EPFL Salamander, Yamor, Blimp) with no product weight.
- 2: not sought (LEGO Mindstorms rover, Robotis Bioloid dog).
- 1 each: not a robot product (NAO sample object); vendor page tab labels not recoverable (TIAGo++); source not opened (NASA Sojourner).

Other limits: a vendor weight is one number with unclear scope more often than not; we recorded the scope words but did not contact any vendor. Some vendor sources are mirrors or archived copies of the vendor's file, marked in the table. Template handling is approximate. Only masses were compared, not inertias or centres of mass. One reading per vendor figure.

## Reproduce

Pinned Webots commit: b3f283949798b8f26a3dcc4df931f71f5148bc93.

```
git clone https://github.com/cyberbotics/webots.git && git -C webots checkout b3f283949798b8f26a3dcc4df931f71f5148bc93
```

Then add up the `Physics` masses of each PROTO under `projects/robots` by the rules in Method, and compare with the vendor figures in the table above. The vendor figures, scope words, links and verdicts were filled in by hand. The scripts used for this pass are not published here.
