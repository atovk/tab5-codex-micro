/* Host test of the production wrapper against real mbedTLS. The TinyCrypt
 * keygen shim below is only a host fixture: the device self-test calls the
 * original pinned archive, which cannot execute on a desktop CPU. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include "../src/ble_ecc_compat.c"

static struct uECC_Curve_t test_curve;
static int keygen_fault;

uECC_Curve uECC_secp256r1(void) { return &test_curve; }

void esp_fill_random(void *output, size_t length) {
    FILE *random = fopen("/dev/urandom", "rb");
    assert(random);
    assert(fread(output, 1, length, random) == length);
    assert(fclose(random) == 0);
}

int uECC_shared_secret(const uint8_t *public_key, const uint8_t *private_key,
                       uint8_t *secret, uECC_Curve curve) {
    /* Mach-O has no GNU --wrap. The actual firmware link is verified separately. */
    return __wrap_uECC_shared_secret(public_key, private_key, secret, curve);
}

int uECC_make_key_with_d(uint8_t *public_key, uint8_t *private_key,
                          unsigned int *native, uECC_Curve curve) {
    assert(curve == uECC_secp256r1());
    if (keygen_fault == 1) return 0;
    for (size_t i = 0; i < 8; ++i) {
        unsigned int word = native[7 - i];
        private_key[i * 4] = word >> 24;
        private_key[i * 4 + 1] = word >> 16;
        private_key[i * 4 + 2] = word >> 8;
        private_key[i * 4 + 3] = word;
    }
    mbedtls_ecp_group group;
    mbedtls_ecp_point point;
    mbedtls_mpi scalar;
    uint8_t output[65];
    size_t length = 0;
    mbedtls_ecp_group_init(&group);
    mbedtls_ecp_point_init(&point);
    mbedtls_mpi_init(&scalar);
    assert(mbedtls_ecp_group_load(&group, MBEDTLS_ECP_DP_SECP256R1) == 0);
    assert(mbedtls_mpi_read_binary(&scalar, private_key, 32) == 0);
    assert(mbedtls_ecp_mul(&group, &point, &scalar, &group.G, ecc_random, NULL) == 0);
    assert(mbedtls_ecp_point_write_binary(&group, &point, MBEDTLS_ECP_PF_UNCOMPRESSED,
                                         &length, output, sizeof(output)) == 0);
    assert(length == 65);
    memcpy(public_key, output + 1, 64);
    mbedtls_mpi_free(&scalar);
    mbedtls_ecp_point_free(&point);
    mbedtls_ecp_group_free(&group);
    if (keygen_fault == 2) public_key[0] ^= 1;
    if (keygen_fault == 3) private_key[0] ^= 1;
    return 1;
}

static void from_hex(const char *hex, uint8_t *bytes, size_t count) {
    assert(strlen(hex) == count * 2);
    for (size_t i = 0; i < count; ++i) {
        unsigned value = 0;
        assert(sscanf(hex + i * 2, "%2x", &value) == 1);
        bytes[i] = value;
    }
}

static void rejected(const uint8_t *public_key, const uint8_t *private_key,
                     uECC_Curve curve) {
    uint8_t secret[32], zero[32] = {0};
    memset(secret, 0xa5, sizeof(secret));
    assert(uECC_shared_secret(public_key, private_key, secret, curve) == 0);
    assert(memcmp(secret, zero, 32) == 0);
}

int main(void) {
    uECC_Curve curve = uECC_secp256r1();
    uint8_t secret[32], scalar[32] = {0}, public_key[64];
    /* RFC 5903 answer, in both directions, catches scalar/XY/X endianness. */
    assert(uECC_shared_secret(kat_peer, kat_private, secret, curve) == 1);
    assert(memcmp(secret, kat_shared, 32) == 0);
    from_hex("C6EF9C5D78AE012A011164ACB397CE2088685D8F06BF9BE0B283AB46476BEE53", scalar, 32);
    assert(uECC_shared_secret(kat_public, scalar, secret, curve) == 1);
    assert(memcmp(secret, kat_shared, 32) == 0);

    /* Small private scalar and fixed-width leading-zero shared X. The latter
     * is independently checked against Python cryptography in the test runner. */
    memset(scalar, 0, 32);
    scalar[31] = 1;
    assert(uECC_shared_secret(kat_peer, scalar, secret, curve) == 1);
    assert(memcmp(secret, kat_peer, 32) == 0);
    from_hex("6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296"
             "4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5", public_key, 64);
    scalar[30] = 1;
    scalar[31] = 123; /* 379 * G */
    assert(uECC_shared_secret(public_key, scalar, secret, curve) == 1);
    uint8_t leading_zero[32];
    from_hex("005543894af3d00ed7d740abdbd75c96b06877b787db5f70eea78b90a8d7c00a", leading_zero, 32);
    assert(memcmp(secret, leading_zero, 32) == 0);

    /* Reject invalid scalar, coordinates outside Fp and non-curve points. */
    memset(scalar, 0, 32);
    rejected(kat_peer, scalar, curve);
    from_hex("ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", scalar, 32);
    rejected(kat_peer, scalar, curve); /* group order */
    scalar[31] += 1;
    rejected(kat_peer, scalar, curve);
    memset(scalar, 0xff, 32);
    rejected(kat_peer, scalar, curve);
    memset(public_key, 0, 64);
    rejected(public_key, kat_private, curve);
    memcpy(public_key, kat_peer, 64);
    public_key[63] ^= 1;
    rejected(public_key, kat_private, curve);
    memcpy(public_key, kat_peer, 64);
    from_hex("ffffffff00000001000000000000000000000000ffffffffffffffffffffffff", public_key, 32);
    rejected(public_key, kat_private, curve);
    rejected(kat_peer, kat_private, NULL);
    rejected(NULL, kat_private, curve);
    rejected(kat_peer, NULL, curve);
    assert(uECC_shared_secret(kat_peer, kat_private, NULL, curve) == 0);

    /* Startup gate checks keygen errors, serialized key mismatch and recovery.
     * This host shim tests the gate; only P4 tests the actual keygen archive. */
    assert(tab5_ble_ecc_self_test());
    for (keygen_fault = 1; keygen_fault <= 3; ++keygen_fault)
        assert(!tab5_ble_ecc_self_test());
    keygen_fault = 0;
    assert(tab5_ble_ecc_self_test());
    puts("ECDH known-answer, invalid input, output clearing and startup gate tests passed");
    return 0;
}
