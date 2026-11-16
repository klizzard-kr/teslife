# Changelog

## v1.15.0

- MIT license, copyright klizzard-kr, 2026. Leaflet stays under its own BSD-2-Clause notice in `web/static/vendor/leaflet/LICENSE`.
- The page version is v1.15.0. The README run instructions no longer name a machine, a bind address, or a screenshot.
- PROCESS_VERSION stays 24. This release does not rewrite stored trips or charges.

## v1.14.11

- Documentation only. `docs/DESIGN.md`, `README.md`, and `docs/DEPLOY-LOG.md` now match the code: process version 24, Naver maps and in-server OSRM, 64 telemetry signals with speed at 1 second and location at 3 seconds / 20 m (four unused motion signals excluded), short-trip distance, charge-session joining, automatic charger choice, charge place names, the `port_open_rates` webhook, the pre-discount average unit price, the monthly full charge, and the 39 psi tire warning. The server does not wake the car. No runtime code changed.

## v1.14.10

- The charge tab's average unit price is the sum of the paid amount and the discount, divided by billed kWh. That matches each charge's own unit price. A period with no discount is unchanged.

## v1.14.9

- A trip whose odometer did not move a full step uses speed samples, then the GPS path, for its distance. Longer trips still use the odometer. The source is stored on new rows. Existing trips are left as they are unless a dry-run script is applied. Under 1 km the screens and the trip-end notice show meters. A very short move says the car changed spots. Efficiency stays hidden for those short distances. PROCESS_VERSION stays 24.

## v1.14.8

- Opening the charge port sends nearby public prices to the bot only. Each operator line has the speed, member price, guest price, roaming price, and distance. There is no app notice and no app push, and a missed bot reply is not pushed either. One parking session sends it once. The five-minute open-port warning is unchanged.
- A charger that changes from waiting to charging after the port opens is stored on the charge. If none or several change, the nearest charger is kept. A few later checks cover a slow public feed. A failed lookup does not stop the charge from being recorded.
- A pause and resume in the same parked session sends the charge-start notice once. PROCESS_VERSION stays 24.

## v1.14.7

- An empty charge place prefers a nearby Naver result whose category is an EV charger. The trailing charger words are removed from that name. Otherwise the building name is used, then the road address. A shop inside the building is not used. Suggestions show the same shortened name without a category tag, and a building value that arrives as an object is read as its text. PROCESS_VERSION stays 24. A dry-run can list how stored places would change; it does not write unless asked.

## v1.14.6

- The charge place field can ask for suggestions again. `/api/place-suggest` is a registered route, so an empty field and a typed query both return JSON. Clearing the field while it stays focused shows the empty suggestions. PROCESS_VERSION stays 24, and stored rows are not rebuilt.

## v1.14.5

- A clear fast-charge signal decides DC before AC energy: FastChargerPresent true, a fast charger type, DC power of 15 kW or more, or charger voltage of 300 V or more. Wall power near 200 V with current and FastChargerPresent false stays AC. Place is not used. PROCESS_VERSION stays 24, so stored charge rows are not rebuilt.

## v1.14.4

- Each notification setting keeps its name on the left and its switch at the right end of the row. The switch stays vertically centered when the name wraps, on a narrow screen as well. A line sits under the main switch and between groups such as trips and charging. Rows inside a group have no line between them.
- The main switch sits to the right of the 알림 heading. Turning it off grays the rows under it and leaves their saved values in place. Those rows cannot be changed until it is on again, and then each kind sends as it was saved.
- When notifications are blocked, the hint names the settings path for the browser in use. iPhone and iPad Home Screen apps point at Settings. A Safari tab says to add the app to the Home Screen. Chrome, Safari on Mac, Edge, Firefox, and Samsung Internet each have their own path. Any other browser gets a general sentence. English uses the same choice.
- An empty charge place field suggests places the owner saved at the same station or within 150 m, then a building name from reverse geocoding. Typing keeps the local search at five results, puts a matching saved place first, and shows the last category step beside each result. A missing key or a failed lookup adds nothing, and no stored row is rewritten.
- A matched charger fills the station name and address only. An empty place copies a place the owner saved at the same station or within 150 m, unless that text is just the station name and address. A first visit asks Naver reverse geocoding once for the building name, then the road address, and leaves the place empty when neither is available. A shop from place search is not used. A place the owner typed, or a pin they moved, is not replaced. Older charge rows are not rewritten.
- Trip alerts are two switches, trip start and trip end. A saved driving switch that was off turns both off. One that was on, or was never saved, leaves both on. The inbox and the bot each follow the matching switch.

## v1.14.3

- A charge pause joins only the next session, and only inside the last 3 hours, while the charge port's last signal is still open and the car stayed put. The port is sent only when it changes. A close signal, a trip, speed, drive gear, a move over 100 m, or five minutes with no signal starts a new session, including on a full rebuild. The end is the moment charging stopped. An existing row keeps its end and kind unless it is that next session. Older charges are not merged or deleted.
- An energy counter that resets is summed from the rise before the reset and the rise after it.
- Wall power near 200 V with charge current and FastChargerPresent false is AC, so a wall session is not stored as DC.
- The charge detail clock is the full start-to-end span. The time next to energy and cost leaves out pauses. A zero-power stretch on the chart is shaded and labeled.
- Mark all read applies to the open inbox tab. A bot-written trip-end notice still shows battery, range, and tire pressure in the trip detail order. Its title and summary stay as written, and the stored row is not rewritten.

## v1.14.2

- A service notice list line and the default push say the days and the action for that item. Stored rows stay as they are, and the detail popup is unchanged.
- A trip-end detail adds battery, remaining range, and the four tire pressures from signals stored at the end of the trip. The list summary names the distance and, when stored, the battery and range. A missing value is left out, and the bot webhook receives the same numbers.
- A tire warning, on the home card and in the cold-pressure alert, starts only when the printed psi is under 39.0. The 42 psi label stays where it is only information. The warning names 39 psi, or that value in bar when the display unit is bar.
- Trip and charge notice details use the same labels and order as those detail screens. Distance, battery, and range come first on a finished trip. Tire pressures stay last. Stored notice rows are not rewritten.
- A parked-open notice names the part, such as the charge port, and a vehicle alert uses a short Korean sentence. Unknown alerts name the category and leave the code out of the summary. The bot can still address a stored id that contains Korean or `|`. Display does not rewrite the row.
- A notice summary does not repeat its title. The title stays a short topic, and the summary is a longer sentence with the value, the cause, or what to do. A missing number is not invented.
- An open charge port after parking waits five minutes. A cable or a charge in that time cancels the notice, including the bot webhook. A port that stays open with no cable is sent once. Stored notices are not rewritten.

## v1.14.1

- A deleted service or full-charge notice stays dismissed until its due date, status, or month changes. The id is kept in `notice_dismissed`. Other tables are not changed.
- Those notices are stamped when they are first created. The due date is a detail row, `기한 9월 19일`. Overdue copy says `N일 지났어요`; time left says `N일 남았어요`.
- The separate full-charge notice is replaced by a monthly 배터리 완충 service item. A charge that ends at 99.5% or more sets that item's last date. Existing consumable, ledger, and charge rows are not rewritten.
- The home `psi` control switches the displayed unit to bar. Stored pressures stay as they are. The choice is remembered in the browser and in `pressure_unit` settings, and a new push uses that unit.
- Bot notice ids may end with `/`, which service notices use. Other unsafe ids are still rejected.

## v1.14.0

- A notice without a detail popup has a trash button on the row. A trip or charge notice is deleted from the trash button beside the detail close button. Both stay aligned with the time on the title line.
- Under the current tab, Delete all asks once, then removes only that tab. Mark all read marks only that tab. One trash press does not ask.
- Deletes remove `notifications` rows, including rows merged into them. A deleted pending notice is not pushed later. Trips, charges, the ledger, service items, and the alert log stay.

## v1.13.1

- The outbound bot webhook adds `Authorization: Bearer` when `BOT_NOTIFY_WEBHOOK_KEY` is set. The HMAC signature and `BOT_PUSH_TOKEN` on `/api/bot/notify` stay as they are. A missing key still posts with the signature only.

## v1.13.0

- When `BOT_NOTIFY_WEBHOOK_URL` and `BOT_PUSH_TOKEN` are both set, a new notice is stored as pending and POSTed as signed JSON (`id`, `kind`, `event_ts`, `values`). The signature is `X-Teslife-Signature: sha256=<hmac-sha256 of the raw body>`, keyed by `BOT_PUSH_TOKEN`. Coordinates and the VIN are left out. A place is only the neighborhood and road.
- `POST /api/bot/notify` with `Authorization: Bearer` can `send` (store the bot's title and summary, then push), `skip` (keep it in the list), `merge` (fold it into another row without deleting either), or `create` a bot notice. `GET /api/bot/notify/pending` lists what is still waiting.
- A pending notice that is still waiting after 10 minutes is pushed with the existing sentence. An empty webhook URL keeps the previous immediate push.

## v1.12.2

- The custom map +/− buttons are gone. Trip, charge, and place maps use the map's own zoom control again (`zoomControl: true`).

## v1.12.1

- The charge-location count sits inside the same circle marker, centered. The separate number marker is gone.

## v1.12.0

- Release notes keep a line between versions only. Each item says, in one short line, what got better.
- The inbox drops the unread background. A new notice is bold in the title, the time sits on that line, and the line under it is a short summary. Old notices are shown as sentences without rewriting stored rows. The time is when the event happened. A push uses the same title and summary and opens that notice.
- The home tire warning compares the psi printed on the tiles with the recommended psi. A colder stored reading does not warn by itself.
- Charge-place search saves the name and the place coordinates together and reloads nearby stations. The count on a charge dot is centered, and the Naver zoom control is a small pair of buttons.
- The charge temperature band is a light step only where samples exist. Start and end percents under the battery bar are gone.

## v1.11.0

- The map uses Naver Dynamic Map when `NAVER_MAPS_CLIENT_ID` is set. Trip routes and charge places stay. With no key, the page keeps the OpenStreetMap map.
- Reverse geocoding uses Naver and falls back to Nominatim. A 시·군 name from that lookup can select the charger catalog. Lookups are cached and retried.
- The charge place field searches Naver through the server. The 시·군 name is added to the query. Results are the nearest five titles. A typed place is kept when the station changes. A clear button empties the field.
- The version label opens release notes. The newest version is expanded.
- `python -m process.send_telemetry` prints the VehicleSpeed 1-second delta and does not post it unless `--send` is passed. This release does not send it.

## v1.10.0

- Notification titles are sentences. The list is left-aligned, unread rows are bold on a light background, and the sheet keeps one height and scrolls inside. Tapping a row shows one value per line from the stored details.
- Settings groups alerts into 주행, 충전, 보안, and 차량. Tire pressure is its own switch. Charge start sits with charging.
- The charge energy graph uses one temperature scale. The band between the low and high reading is held until the next sample.
- Public charger catalogs are requested by 시·군 (`zscode`) from the offline boundary file `process/data/sigungu.json`. Pages continue until the catalog is finished. A failed page is retried. JSON is read from the outer object (`resultCode`, `totalCount`, `items.item`) and from `response.body`. A positive total with an empty list does not mark the region done. A finished 시·군 that this parser has not marked, or whose saved chargers are under half of `total_count`, is fetched once. The status API is not used for this.
- Parked 시·군 catalogs refresh once each day. Driving into a new 시·군 fetches it then. A 시·군 response is stored in full, not only within 1 km of the car. Opening the charge port fetches again when nothing is stored within 300 m of the port. Charge-end matching uses the catalog already stored.
- A charge the owner saved (`source` user) is not rematched. The edit list shows stations within 300 m. Automatic matching stays within 100 m.

## v1.9.0

- Charge detail from the ledger opens a session that crosses midnight.
- Plug Link charges with an empty payment choice become 회원. The update does not add or remove rows.
- A shorter catalog station name is stored as the longer name already saved for that vehicle, and an empty operator is filled from that station. A payment choice or station name already saved is left as it is.
- The 보유 카드 setting is gone. A new charge defaults to 비회원, then the last choice for that operator.
- Opening the charge port names the nearby operator and this car's rate. A roaming price is included only when it is cheaper.
- A push opens its detail and marks that row read. The title is the short title; the body is one line. The inbox list truncates that line. The popup is one value per row.
- A webhook failure still writes the inbox row and sends the app push.
- Low tires, a slow leak, and tire-set alerts get a push and an inbox row.
- Charge complete, a service item, and the end of a trip include the figures named in the release notes.
- The settings icon is a gear with a hole. The inbox dot sits on top of the title so read and unread titles share a left edge.
- Inbox tabs are 주행, 충전, 보안, and 차량, each with its unread count. Rows use the event time.
- Trip and charge energy details list battery minimum and maximum temperature on their own rows. A fan that is off shows 꺼짐. Fan strength uses the step, not the trip maximum.
- Average and maximum charging power, and the power graph, sit in 에너지 상세. The charger section keeps the rated output.
- Tire changes are one 타이어 교체 screen. The set on the car can be registered on its own. Mount dates and odometers can be edited. A new or edited mount cannot overlap an existing interval, including one still on the car.
- Tire prices do not create, change, or delete 차계부 rows.
- The home status tiles are 배터리, 주행가능거리, 소프트웨어, and 현재 주행거리. The software tile shows the current version and ↑ when a newer one is waiting.
- A software update is announced once per version, in the 차량 tab. A whitespace-only update version is not an update. An install percent is ignored when the download is 0 or no update is available.

The app does not add "from teslife" to a push title. A browser or the phone may still show the site name beside it.
