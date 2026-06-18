# PetKit fountain BLE debugging guide

When the PetKit app shows "failed to connect".

## Scope

Fountain-focused, but most of the guide also applies to other PetKit BLE
devices (feeders with BLE, litter robots, etc.):

- The Quick fix and Steps B, C, D are general PetKit-cloud and
  PetKit-app behavior.
- Step A is fountain-only. PR #203's local BLE coordinator filters on
  `WaterFountain`, so `connection_mode` does not affect other devices.
- The CMD codes referenced (213, 73, 86, 84, 210, 211) are the fountain
  handshake. Other devices use different codes.

## Identifying your fountain

PetKit fountains use Telink chips, so the MAC starts with `A4:C1:38`.
The remaining three bytes are unique per fountain. Find yours on the
sticker under the device or in `bluetoothctl scan le` output. The BLE
name follows `Petkit_<MODEL>_<NNN>`, where MODEL is `CTW3`, `CTW3UV`,
`CTW2`, `W5`, `W4X`, etc.

Examples below assume MAC `A4:C1:38:XX:XX:XX` and name `Petkit_<MODEL>_<NNN>`.
Substitute your own.

## Home Assistant Bluetooth advertisement diagnostics

The integration logs PETKIT advertisements that Home Assistant already sees
through local Bluetooth adapters or ESPHome Bluetooth Proxies. This is
diagnostic-only. It does not update entity state and does not replace the
existing PetKit cloud/API BLE relay path.

Enable debug logging:

```yaml
logger:
  default: info
  logs:
    custom_components.petkit: debug
    pypetkitapi.bluetooth: debug
```

For an Eversweet 3 Pro UVC visible as `Petkit_W4XUVC`, expect lines like:

```text
PETKIT HA Bluetooth advertisement discovery path (cache): matched address=A4:C1:38:60:7E:8E local_name=Petkit_W4XUVC rssi=-49 source=D4:E9:F4:74:E6:4A connectable=True service_uuids=['5b70bdd8-91bf-e789-9f4d-720508e63ebb'] manufacturer_data_keys=[] service_data_keys=['00000000-0000-1000-8000-00805f9b34fb', '0000c1a4-0000-1000-8000-00805f9b34fb']
PETKIT HA Bluetooth advertisement discovery path (cache): Eversweet 3 Pro UVC candidate visible as Petkit_W4XUVC at A4:C1:38:60:7E:8E
```

If the PetKit cloud relay path also logs `No BLE relay devices found.`, that
means PetKit's own relay-discovery endpoint did not return a relay device.
It does not mean Home Assistant Bluetooth or the ESPHome proxy failed to see
the fountain. These advertisement logs confirm visibility, but they do not
expose decoded water or filter state.

When advertisements are visible but do not expose enough state, call the
`petkit.inspect_bluetooth_gatt` service with the fountain address:

```yaml
action: petkit.inspect_bluetooth_gatt
data:
  address: A4:C1:38:60:7E:8E
  max_read_bytes: 128
```

Expected diagnostic output:

```text
PETKIT HA Bluetooth GATT diagnostics: connecting address=A4:C1:38:60:7E:8E local_name=Petkit_W4XUVC source=D4:E9:F4:74:E6:4A
PETKIT HA Bluetooth GATT diagnostics: service uuid=...
PETKIT HA Bluetooth GATT diagnostics: characteristic uuid=... handle=... properties=['read', 'write-without-response'] description=...
PETKIT HA Bluetooth GATT diagnostics: read uuid=... len=... truncated=False hex=...
PETKIT HA Bluetooth GATT diagnostics: disconnected address=A4:C1:38:60:7E:8E
```

This opens a short direct BLE connection, so avoid running it while the PetKit
app is actively connected to the fountain.

## Quick fix to try first: connect from another phone

If one phone keeps getting "failed to connect", have a different phone with
access to the same fountain (via PetKit account sharing) open the app and
connect. After that succeeds, the originally failing phone usually works
on the next try.

Confirmed twice. Try this before walking through the steps below.

### Why it works

PetKit cloud keeps an active-session binding per fountain. Failed connects
can leave that binding stale. Any phone that completes a cloud session
forces a refresh, and the original phone sees clean state next time.

### Why HA local BLE does not unstick it

The local-BLE coordinator talks straight to the fountain (CMD 213, 73, 86,
84, 210, 211). None of that touches PetKit cloud, so the binding table
never sees HA.

A local poll can also create the stuck state. The CMD 73 auth handshake
boots any existing cloud-relay session, but HA does not register a
replacement on the cloud side. The binding stays stale until an app
session refreshes it.

## Where to run the commands below

Install the **Terminal & SSH** add-on (Settings -> Add-ons -> Add-on Store)
and open it. That gives you a shell on Home Assistant OS where every
command in this guide runs as-is. `bluetoothctl` is the Linux Bluetooth
CLI, preinstalled on HA OS. `ha core ...` is the supervisor CLI, also
preinstalled.

If you SSH in from another machine, prefix each command with
`ssh <your-ha-host>` instead.

## Symptom map

```
"failed to connect" in app
        |
        v
Try the quick fix above. Worked -> done. Failed -> [1]
        |
        v
[1] Is HA polling over local BLE?
    YES -> HA blocks. One BLE master at a time. Toggle off + restart. Step A.
    NO  -> [2]
        |
        v
[2] Is the fountain advertising?
    NO  -> Fountain side. Power cycle properly. Step B.
    YES -> Phone side. Step C.
```

## Step A: verify HA is not blocking

### Check current connection mode

```bash
python3 -c '
import json
data = json.load(open("/config/.storage/core.config_entries"))
for e in data["data"]["entries"]:
    if e["domain"] == "petkit":
        print(json.dumps(e["options"].get("local_ble_options",{}), indent=2))
'
```

(Or just open the integration in the UI: Settings -> Devices & services
-> PetKit -> Configure. The Local BLE section shows the same fields.)

- `connection_mode: cloud_only` -> HA does not poll BLE. OK.
- `connection_mode: ble_only` or `auto` -> HA polls. That is the problem.
- Legacy `local_ble_enabled: true` is the same as `auto`. Migration to v9
  should have replaced it.

### Check if HA is actively trying to connect

```bash
ha core logs 2>&1 | grep -iE 'A4:C1:38|fountain_ble|local_ble' | tail -15
# Replace A4:C1:38 with your full MAC if you want to filter to one fountain.
```

Empty when local BLE is off. `BLE sending init cmd to A4:C1:38:...`
means HA is still polling.

### Fix

1. Settings -> Devices & services -> PetKit -> Configure -> Local BLE,
   set `connection_mode` to `cloud_only`
2. `ha core restart` (toggle alone has not always been enough, see
   Known issue)
3. Re-grep the log. Should be empty.

### Known issue (PR #203)

Before the unload-teardown fix landed in PR #203, the old coordinator
kept polling in parallel after an options change. After that fix,
toggling without a restart should be enough. Verify by grepping the log
after a toggle.

## Step B: verify the fountain is awake and connectable

### Scan

`bluetoothctl` opens an interactive shell. The one-shot form below scans
for 12 seconds, then exits, then greps the result.

```bash
bluetoothctl --timeout 12 scan le 2>&1 | grep -iE 'A4:C1:38|petkit|eversweet'
```

- Empty: fountain is not advertising, or the host BT radio is too far
  away. HA actually reaches the device through the ESPHome proxy, so a
  host scan is a sanity check, not a presence test.
- `Device A4:C1:38:XX:XX:XX RSSI: ...`: radio is awake.

### Details

```bash
bluetoothctl info A4:C1:38:XX:XX:XX
```

Expected:

- `Name: Petkit_<MODEL>_<NNN>`
- `Paired: no, Bonded: no` is normal. PetKit uses GATT without classic
  pairing. Do not unpair in Android/iOS BT settings.
- `RSSI`: -85 to -95 dBm is normal from the host. A phone at 1-3 m will
  be -50 to -70.

### If the fountain is not advertising

1. USB out, battery out, wait 60 seconds (30 has not always been long
   enough to clear stuck state)
2. Battery in, USB in
3. Tap the fountain. Green LED = awake.
4. Re-scan.

### Host cannot complete `connect`

Expected at -85 dBm and worse. Host RSSI says nothing about phone RSSI.
Not a useful test for "can the app connect".

## Step C: phone and app side

When HA is quiet (Step A) and the fountain advertises (Step B) but the
app still fails.

### What helps

1. App permissions, especially on Samsung. Settings -> Apps -> PetKit ->
   Permissions:
   - Location: "Allow". Android needs it for BLE scan.
   - Nearby devices: "Allow"
     One UI revokes both silently after inactivity.
2. Clear cache (not Clear data). Settings -> Apps -> PetKit -> Storage
   -> Clear cache. Drinking history and filter stats live on the
   fountain and PetKit cloud, not the app cache. Safe.
3. Battery. Settings -> Apps -> PetKit -> Battery -> Unrestricted.
   Samsung throttles BLE APIs in the background otherwise.
4. Force-stop the app after permission or cache changes, then reopen.
5. Distance: under 2 m line of sight. 3 m still tends to work.

### Reinstall (last resort)

Login and stats live cloud-side. Account sharing hangs off the account,
not the install. Reinstalling can fix corrupt caches, stuck Play
Services BLE bindings, and stale BLE handles in the app process.

### Not worth trying

- Forget/unpair in Settings -> Bluetooth. The fountain is never paired.
  PetKit uses custom GATT, not a standard profile.
- Clearing the Bluetooth cache. PetKit does not use bonding state.
- Toggling phone BT off/on more than once. If once does not help, this
  is not the problem.

### Cloud check

The app often requires a cloud check before BLE:

- Fountain is in the user's device list in the app
- Account sharing from the owner is active

If cloud state is wrong, "failed to connect" can mask a binding problem
rather than a BLE problem.

## Step D: isolation test

When the steps above fail and you cannot tell if it is the fountain or
the phone.

Two accounts in play: one owner (registered the fountain) and one
family-share member. The tests below refer to phones, not roles.

### Test 1: another phone

Another phone with access to the same fountain opens the app.

- Works: fountain and cloud are fine. Original phone or its account state
  is the issue. Go to Test 2.
- Fails: fountain or cloud is broken. Contact PetKit or replace.

### Test 2: login swap

Sign into a different account on the original phone.

- Works: phone is fine. Original account state is corrupt. Reinstall
  and log in from scratch.
- Fails: phone is the problem. BLE stack, OS state, vendor bug.
  Reinstall and a BT-stack reset are the last steps before suspecting
  hardware.

## Common questions

**Q: Can HA and a phone connect at the same time?**
No. The fountain accepts one BLE master.

**Q: Does the ESP32 BLE relay matter?**
`bluetooth_options.ble_relay_enabled` controls cloud-API relay over an
ESPHome bluetooth-proxy. The proxy can also hold an active connection.
If it is on, check the log for `bluetooth_proxy` plus the fountain MAC.
Off or unplugged: irrelevant.

**Q: Log shows `Cannot connect to host api.eu-pet.com:443`.**
Cloud poll, unrelated to local BLE. Usually a DNS glitch right after
restart. Persistent: separate issue (DNS, firewall, PetKit API down).

**Q: `last_ble_connection` sensor is `unknown`.**
Expected when local BLE is off. Updates only on successful local polls.

## Quick checklist

```
[ ] Tried the quick fix (connect from another phone first)
[ ] Have a shell on HA OS (Terminal & SSH add-on or your own SSH)
[ ] options.local_ble_options.connection_mode = cloud_only
[ ] ha core logs has no A4:C1:38 / fountain_ble / local_ble hits
[ ] bluetoothctl scan sees Petkit_<MODEL>_<NNN> (or accept the host is too far)
[ ] bluetoothctl info shows Name: Petkit_<MODEL>_<NNN>
[ ] Green LED on the fountain when tapped
[ ] Phone has Location + Nearby devices permission for PetKit
[ ] PetKit app battery is Unrestricted
[ ] Phone is < 3 m line of sight
[ ] Fountain is in the app's device list
```
