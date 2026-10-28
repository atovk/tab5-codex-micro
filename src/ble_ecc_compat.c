/* SPDX-License-Identifier: MIT
 *
 * Narrow compatibility fix for the pinned ESP-IDF TinyCrypt P-256 ECDH bug.
 * The affected archive passes a regularized 257-bit scalar to a 256-bit-only
 * hardware path. Keep TinyCrypt key generation and the BLE protocol, but route
 * shared-secret computation through the SDK's checked mbedTLS ECDH API.
 * mbedTLS may use its own hardware port; this is not a software-only fallback.
 *
 * Upstream diagnosis/fix (no upstream implementation is copied here):
 * https://github.com/espressif/esp-hosted-mcu/issues/241
 * https://github.com/espressif/esp-idf/commit/9fd7cb7e606a06111e1b14be7f4e00d77d9cf3dd
 * Build identity and libbt.a digest are enforced by tools/ble_ecc_patch.py.
 */

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "sdkconfig.h"
#include "esp_idf_version.h"
#include "esp_random.h"
#include "mbedtls/ecdh.h"
#include "mbedtls/platform_util.h"
#include "tinycrypt/ecc_dh.h"

#if !defined(TAB5_BLE_ECC_PINNED) || TAB5_BLE_ECC_PINNED != 1
#error "Tab5 ECC compatibility must be enabled by the pinned SDK build guard"
#endif
#if !CONFIG_IDF_TARGET_ESP32P4 || ESP_IDF_VERSION != ESP_IDF_VERSION_VAL(5, 5, 5)
#error "Tab5 ECC compatibility requires the reviewed ESP32-P4 IDF 5.5.5 build"
#endif
#if !CONFIG_BT_SMP_CRYPTO_STACK_TINYCRYPT || !CONFIG_BT_NIMBLE_SM_SC || !CONFIG_MBEDTLS_ECDH_C
#error "Tab5 ECC compatibility requires TinyCrypt Secure Connections and mbedTLS ECDH"
#endif

/* Exported by the pinned archive, even though its declaration is normally
 * hidden behind ENABLE_TESTS. Used only for the startup known-answer test. */
extern int uECC_make_key_with_d(uint8_t *, uint8_t *, unsigned int *, uECC_Curve);

_Static_assert(sizeof(unsigned int) == 4 && NUM_ECC_BYTES == 32,
               "Unexpected TinyCrypt P-256 word layout");

static int ecc_random(void *context, unsigned char *output, size_t length) {
    (void)context;
    esp_fill_random(output, length);
    return 0;
}

int __wrap_uECC_shared_secret(const uint8_t *public_key,
                              const uint8_t *private_key,
                              uint8_t *secret, uECC_Curve curve) {
    if (!secret) return 0;
    /* TinyCrypt's ABI supplies separate buffers. Clear failure output even for
     * invalid curve / NULL input, before allocating any crypto contexts. */
    memset(secret, 0, 32);
    if (!public_key || !private_key || curve != uECC_secp256r1()) return 0;

    int success = 0;
    uint8_t encoded_point[65];
    mbedtls_ecp_group group;
    mbedtls_ecp_point peer;
    mbedtls_mpi scalar, shared;
    mbedtls_ecp_group_init(&group);
    mbedtls_ecp_point_init(&peer);
    mbedtls_mpi_init(&scalar);
    mbedtls_mpi_init(&shared);

    encoded_point[0] = 0x04; /* TinyCrypt supplies big-endian X || Y. */
    memcpy(encoded_point + 1, public_key, 64);
    if (mbedtls_ecp_group_load(&group, MBEDTLS_ECP_DP_SECP256R1) != 0 ||
        mbedtls_mpi_read_binary(&scalar, private_key, 32) != 0 ||
        mbedtls_ecp_check_privkey(&group, &scalar) != 0 ||
        mbedtls_ecp_point_read_binary(&group, &peer, encoded_point, sizeof(encoded_point)) != 0 ||
        mbedtls_ecp_check_pubkey(&group, &peer) != 0 ||
        mbedtls_ecdh_compute_shared(&group, &shared, &peer, &scalar, ecc_random, NULL) != 0 ||
        mbedtls_mpi_write_binary(&shared, secret, 32) != 0) {
        goto cleanup;
    }
    success = 1;

cleanup:
    if (!success) mbedtls_platform_zeroize(secret, 32);
    mbedtls_mpi_free(&shared);
    mbedtls_mpi_free(&scalar);
    mbedtls_ecp_point_free(&peer);
    mbedtls_ecp_group_free(&group);
    mbedtls_platform_zeroize(encoded_point, sizeof(encoded_point));
    return success;
}

/* RFC 5903 section 8.1 P-256 numerical test vectors. Attribution and the
 * accompanying RFC code-component license are in tools/vendor/RFC5903-LICENSE.
 * These are public test keys, never production pairing keys. */
static const uint8_t kat_private[32] = {
    0xc8,0x8f,0x01,0xf5,0x10,0xd9,0xac,0x3f,0x70,0xa2,0x92,0xda,0xa2,0x31,0x6d,0xe5,
    0x44,0xe9,0xaa,0xb8,0xaf,0xe8,0x40,0x49,0xc6,0x2a,0x9c,0x57,0x86,0x2d,0x14,0x33,
};
static const uint8_t kat_public[64] = {
    0xda,0xd0,0xb6,0x53,0x94,0x22,0x1c,0xf9,0xb0,0x51,0xe1,0xfe,0xca,0x57,0x87,0xd0,
    0x98,0xdf,0xe6,0x37,0xfc,0x90,0xb9,0xef,0x94,0x5d,0x0c,0x37,0x72,0x58,0x11,0x80,
    0x52,0x71,0xa0,0x46,0x1c,0xdb,0x82,0x52,0xd6,0x1f,0x1c,0x45,0x6f,0xa3,0xe5,0x9a,
    0xb1,0xf4,0x5b,0x33,0xac,0xcf,0x5f,0x58,0x38,0x9e,0x05,0x77,0xb8,0x99,0x0b,0xb3,
};
static const uint8_t kat_peer[64] = {
    0xd1,0x2d,0xfb,0x52,0x89,0xc8,0xd4,0xf8,0x12,0x08,0xb7,0x02,0x70,0x39,0x8c,0x34,
    0x22,0x96,0x97,0x0a,0x0b,0xcc,0xb7,0x4c,0x73,0x6f,0xc7,0x55,0x44,0x94,0xbf,0x63,
    0x56,0xfb,0xf3,0xca,0x36,0x6c,0xc2,0x3e,0x81,0x57,0x85,0x4c,0x13,0xc5,0x8d,0x6a,
    0xac,0x23,0xf0,0x46,0xad,0xa3,0x0f,0x83,0x53,0xe7,0x4f,0x33,0x03,0x98,0x72,0xab,
};
static const uint8_t kat_shared[32] = {
    0xd6,0x84,0x0f,0x6b,0x42,0xf6,0xed,0xaf,0xd1,0x31,0x16,0xe0,0xe1,0x25,0x65,0x20,
    0x2f,0xef,0x8e,0x9e,0xce,0x7d,0xce,0x03,0x81,0x24,0x64,0xd0,0x4b,0x94,0x42,0xde,
};

bool tab5_ble_ecc_self_test(void) {
    bool success = false;
    uint8_t public_key[64] = {0}, private_key[32] = {0}, shared[32] = {0};
    uint8_t invalid[64] = {0};
    unsigned int native_private[8];
    /* uECC test keygen consumes least-significant word first, whereas its
     * serialized key ABI and mbedTLS both use big-endian byte strings. */
    for (size_t i = 0; i < 8; ++i) {
        const uint8_t *p = kat_private + 28 - 4 * i;
        native_private[i] = ((unsigned int)p[0] << 24) |
                            ((unsigned int)p[1] << 16) |
                            ((unsigned int)p[2] << 8) | p[3];
    }
    uECC_Curve curve = uECC_secp256r1();
    /* Exercise the unchanged TinyCrypt keygen hardware path as well as the
     * linker-routed ECDH call used by NimBLE. No RNG callback is replaced. */
    if (uECC_make_key_with_d(public_key, private_key, native_private, curve) != 1 ||
        memcmp(public_key, kat_public, sizeof(public_key)) != 0 ||
        memcmp(private_key, kat_private, sizeof(private_key)) != 0 ||
        uECC_shared_secret(kat_peer, private_key, shared, curve) != 1 ||
        memcmp(shared, kat_shared, sizeof(shared)) != 0) goto cleanup;
    memset(shared, 0xa5, sizeof(shared));
    if (uECC_shared_secret(invalid, private_key, shared, curve) != 0 ||
        memcmp(shared, invalid, sizeof(shared)) != 0) goto cleanup;
    memset(shared, 0xa5, sizeof(shared));
    if (uECC_shared_secret(kat_peer, invalid, shared, curve) != 0 ||
        memcmp(shared, invalid, sizeof(shared)) != 0) goto cleanup;
    success = true;

cleanup:
    mbedtls_platform_zeroize(native_private, sizeof(native_private));
    mbedtls_platform_zeroize(private_key, sizeof(private_key));
    mbedtls_platform_zeroize(shared, sizeof(shared));
    return success;
}
