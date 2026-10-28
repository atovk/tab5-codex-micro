"""Compile project-local Arduino BLE glue with explicit repair authorization.

Arduino-ESP32 3.3.12 automatically deletes an existing bond when a peer asks
to pair again. Tab5 requires a physical, time-limited pairing window instead.
The application hook must be fast and may only inspect connection policy.
Absent hooks fail closed. Also bound the NimBLE synchronization wait so a
missing controller cannot permanently occupy the BLE initialization worker.
NimBLE's UUID de-duplication also drops the distinct HOGP report objects; keep
each object so the service's existing GATT serializer registers every report.

Framework sources are never modified. The timeout does not tear down the
host task: callers must not retry initialization until the device restarts.
"""

from pathlib import Path


SERVER_INCLUDE = "#include <services/gatt/ble_svc_gatt.h>\n"
SERVER_GUARD = """      /* Delete the old bond. */
      rc = ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc);
      if (rc != 0) {
        return BLE_GAP_REPEAT_PAIRING_IGNORE;
      }

      ble_store_util_delete_peer(&desc.peer_id_addr);
"""
DEVICE_WAIT = """  while (!m_synced) {
    ble_npl_time_delay(1);
  }
"""
SERVICE_CHARACTERISTIC_INSERT = """#if defined(CONFIG_NIMBLE_ENABLED)
  if (pExisting != nullptr) {
    pExisting->m_removed = 0;
  } else
#endif
  {
    // Remember this characteristic in our map of characteristics.  At this point, we can lookup by UUID
    // but not by handle.  The handle is allocated to us on the ESP_GATTS_ADD_CHAR_EVT.
    m_characteristicMap.setByUUID(pCharacteristic, pCharacteristic->getUUID());
  }
"""
SERVICE_OBJECT_DEFINITION = """          pChr_a[i].uuid = (const ble_uuid_t *)&(pCharacteristic->m_bleUUID.getNative()->u);
          pChr_a[i].access_cb = BLECharacteristic::handleGATTServerEvent;
          pChr_a[i].arg = pCharacteristic;
          pChr_a[i].flags = pCharacteristic->m_properties;
          pChr_a[i].min_key_size = 0;
          pChr_a[i].val_handle = &pCharacteristic->m_handle;
"""


def replace_once(source, anchor, replacement, name):
    count = source.count(anchor)
    if count != 1:
        raise RuntimeError(
            "Tab5 BLE patch requires Arduino-ESP32 3.3.12; "
            f"expected one {name} anchor, found {count}"
        )
    return source.replace(anchor, replacement, 1)


def patch_server_source(source):
    source = replace_once(
        source, SERVER_INCLUDE,
        SERVER_INCLUDE
        + '\nextern "C" bool tab5_ble_allow_repair(uint16_t conn_handle) __attribute__((weak));\n',
        "NimBLE include",
    )
    return replace_once(
        source, SERVER_GUARD,
        """      /* Preserve existing trust unless the physical pairing window permits repair. */
      rc = ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc);
      if (rc != 0) {
        return BLE_GAP_REPEAT_PAIRING_IGNORE;
      }

      // A global GAP listener cannot override this callback's return value.
      // Fail closed if the application does not provide an authorization hook.
      if (!tab5_ble_allow_repair || !tab5_ble_allow_repair(desc.conn_handle)) {
        return BLE_GAP_REPEAT_PAIRING_IGNORE;
      }
      ble_store_util_delete_peer(&desc.peer_id_addr);
""",
        "repeat-pairing authorization",
    )


def patch_device_source(source):
    return replace_once(
        source, DEVICE_WAIT,
        """  const uint32_t tab5SyncStarted = millis();
  while (!m_synced) {
    if (uint32_t(millis() - tab5SyncStarted) >= 10000u) {
      // Do not tear down a possibly active host task here. The application
      // treats this as a terminal startup failure until restart; USB stays up.
      log_e("Tab5 NimBLE host synchronization timed out");
      return false;
    }
    ble_npl_time_delay(1);
  }
""",
        "NimBLE synchronization wait",
    )


def patch_service_source(source):
    # Require the pinned serializer which emits separate per-object GATT
    # definitions, including their descriptors and value-handle destinations.
    source = replace_once(
        source, SERVICE_OBJECT_DEFINITION, SERVICE_OBJECT_DEFINITION,
        "per-object NimBLE characteristic definition",
    )
    return replace_once(
        source, SERVICE_CHARACTERISTIC_INSERT,
        """#if defined(CONFIG_NIMBLE_ENABLED)
  // HOGP uses one 0x2a4d object per report ID/type. UUID equality does not
  // identify the same characteristic. Restore only the supplied object.
  pCharacteristic->m_removed = 0;
#endif
  // The map is keyed by BLECharacteristic*: inserting the same object is
  // idempotent, while distinct objects with the same UUID remain registered.
  m_characteristicMap.setByUUID(pCharacteristic, pCharacteristic->getUUID());
""",
        "NimBLE characteristic object insertion",
    )


def write_if_changed(destination, content):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_text(encoding="utf-8") != content:
        destination.write_text(content, encoding="utf-8")


def register_platformio(environment):
    if environment.BoardConfig().get("build.mcu") != "esp32p4":
        raise RuntimeError("The Tab5 BLE patch is only supported for ESP32-P4")
    framework = Path(environment.PioPlatform().get_package_dir("framework-arduinoespressif32"))
    sources = framework / "libraries" / "BLE" / "src"
    # Generated files retain the original quoted sibling includes. Set this
    # before source enumeration so middleware never changes compiler signatures
    # midway through building the library.
    environment.AppendUnique(CPPPATH=[str(sources)])
    patches = {"BLEServer.cpp": patch_server_source,
               "BLEDevice.cpp": patch_device_source,
               "BLEService.cpp": patch_service_source}

    def use_project_ble_glue(build_env, node):
        original = Path(node.srcnode().get_abspath())
        if original.resolve() != (sources / original.name).resolve():
            return node
        patch = patches[original.name]
        destination = Path(build_env.subst("$BUILD_DIR")) / "tab5-ble-core" / original.name
        write_if_changed(destination, patch(original.read_text(encoding="utf-8")))
        print("Tab5 BLE: compiling guarded %s" % original.name)
        return build_env.File(str(destination))

    for name in patches:
        environment.AddBuildMiddleware(use_project_ble_glue, "*" + name)


try:
    Import("env")
except NameError:
    pass
else:
    register_platformio(env)
