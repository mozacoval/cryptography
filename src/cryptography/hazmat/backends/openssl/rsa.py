# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import absolute_import, division, print_function

import math

from cryptography import utils
from cryptography.exceptions import (
    AlreadyFinalized, InvalidSignature, UnsupportedAlgorithm, _Reasons
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import (
    AsymmetricSignatureContext, AsymmetricVerificationContext, rsa
)
from cryptography.hazmat.primitives.asymmetric.padding import (
    AsymmetricPadding, MGF1, OAEP, PKCS1v15, PSS
)
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKeyWithNumbers, RSAPrivateKeyWithSerialization,
    RSAPublicKeyWithNumbers
)


def _get_rsa_pss_salt_length(pss, key_size, digest_size):
    salt = pss._salt_length

    if salt is MGF1.MAX_LENGTH or salt is PSS.MAX_LENGTH:
        # bit length - 1 per RFC 3447
        emlen = int(math.ceil((key_size - 1) / 8.0))
        salt_length = emlen - digest_size - 2
        assert salt_length >= 0
        return salt_length
    else:
        return salt


def _enc_dec_rsa(backend, key, data, padding):
    if not isinstance(padding, AsymmetricPadding):
        raise TypeError("Padding must be an instance of AsymmetricPadding.")

    if isinstance(padding, PKCS1v15):
        padding_enum = backend._lib.RSA_PKCS1_PADDING
    elif isinstance(padding, OAEP):
        padding_enum = backend._lib.RSA_PKCS1_OAEP_PADDING
        if not isinstance(padding._mgf, MGF1):
            raise UnsupportedAlgorithm(
                "Only MGF1 is supported by this backend.",
                _Reasons.UNSUPPORTED_MGF
            )

        if not isinstance(padding._mgf._algorithm, hashes.SHA1):
            raise UnsupportedAlgorithm(
                "This backend supports only SHA1 inside MGF1 when "
                "using OAEP.",
                _Reasons.UNSUPPORTED_HASH
            )

        if padding._label is not None and padding._label != b"":
            raise ValueError("This backend does not support OAEP labels.")

        if not isinstance(padding._algorithm, hashes.SHA1):
            raise UnsupportedAlgorithm(
                "This backend only supports SHA1 when using OAEP.",
                _Reasons.UNSUPPORTED_HASH
            )
    else:
        raise UnsupportedAlgorithm(
            "{0} is not supported by this backend.".format(
                padding.name
            ),
            _Reasons.UNSUPPORTED_PADDING
        )

    if backend._lib.Cryptography_HAS_PKEY_CTX:
        return _enc_dec_rsa_pkey_ctx(backend, key, data, padding_enum)
    else:
        return _enc_dec_rsa_098(backend, key, data, padding_enum)


def _enc_dec_rsa_pkey_ctx(backend, key, data, padding_enum):
    if isinstance(key, _RSAPublicKey):
        init = backend._lib.EVP_PKEY_encrypt_init
        crypt = backend._lib.Cryptography_EVP_PKEY_encrypt
    else:
        init = backend._lib.EVP_PKEY_decrypt_init
        crypt = backend._lib.Cryptography_EVP_PKEY_decrypt

    pkey_ctx = backend._lib.EVP_PKEY_CTX_new(
        key._evp_pkey, backend._ffi.NULL
    )
    assert pkey_ctx != backend._ffi.NULL
    pkey_ctx = backend._ffi.gc(pkey_ctx, backend._lib.EVP_PKEY_CTX_free)
    res = init(pkey_ctx)
    assert res == 1
    res = backend._lib.EVP_PKEY_CTX_set_rsa_padding(
        pkey_ctx, padding_enum)
    assert res > 0
    buf_size = backend._lib.EVP_PKEY_size(key._evp_pkey)
    assert buf_size > 0
    outlen = backend._ffi.new("size_t *", buf_size)
    buf = backend._ffi.new("char[]", buf_size)
    res = crypt(pkey_ctx, buf, outlen, data, len(data))
    if res <= 0:
        _handle_rsa_enc_dec_error(backend, key)

    return backend._ffi.buffer(buf)[:outlen[0]]


def _enc_dec_rsa_098(backend, key, data, padding_enum):
    if isinstance(key, _RSAPublicKey):
        crypt = backend._lib.RSA_public_encrypt
    else:
        crypt = backend._lib.RSA_private_decrypt

    key_size = backend._lib.RSA_size(key._rsa_cdata)
    assert key_size > 0
    buf = backend._ffi.new("unsigned char[]", key_size)
    res = crypt(len(data), data, buf, key._rsa_cdata, padding_enum)
    if res < 0:
        _handle_rsa_enc_dec_error(backend, key)

    return backend._ffi.buffer(buf)[:res]


def _handle_rsa_enc_dec_error(backend, key):
    errors = backend._consume_errors()
    assert errors
    assert errors[0].lib == backend._lib.ERR_LIB_RSA
    if isinstance(key, _RSAPublicKey):
        assert (errors[0].reason ==
                backend._lib.RSA_R_DATA_TOO_LARGE_FOR_KEY_SIZE)
        raise ValueError(
            "Data too long for key size. Encrypt less data or use a "
            "larger key size."
        )
    else:
        decoding_errors = [
            backend._lib.RSA_R_BLOCK_TYPE_IS_NOT_01,
            backend._lib.RSA_R_BLOCK_TYPE_IS_NOT_02,
        ]
        if backend._lib.Cryptography_HAS_RSA_R_PKCS_DECODING_ERROR:
            decoding_errors.append(backend._lib.RSA_R_PKCS_DECODING_ERROR)

        assert errors[0].reason in decoding_errors
        raise ValueError("Decryption failed.")


@utils.register_interface(AsymmetricSignatureContext)
class _RSASignatureContext(object):
    def __init__(self, backend, private_key, padding, algorithm):
        self._backend = backend
        self._private_key = private_key

        if not isinstance(padding, AsymmetricPadding):
            raise TypeError("Expected provider of AsymmetricPadding.")

        self._pkey_size = self._backend._lib.EVP_PKEY_size(
            self._private_key._evp_pkey
        )

        if isinstance(padding, PKCS1v15):
            if self._backend._lib.Cryptography_HAS_PKEY_CTX:
                self._finalize_method = self._finalize_pkey_ctx
                self._padding_enum = self._backend._lib.RSA_PKCS1_PADDING
            else:
                self._finalize_method = self._finalize_pkcs1
        elif isinstance(padding, PSS):
            if not isinstance(padding._mgf, MGF1):
                raise UnsupportedAlgorithm(
                    "Only MGF1 is supported by this backend.",
                    _Reasons.UNSUPPORTED_MGF
                )

            # Size of key in bytes - 2 is the maximum
            # PSS signature length (salt length is checked later)
            assert self._pkey_size > 0
            if self._pkey_size - algorithm.digest_size - 2 < 0:
                raise ValueError("Digest too large for key size. Use a larger "
                                 "key.")

            if not self._backend._mgf1_hash_supported(padding._mgf._algorithm):
                raise UnsupportedAlgorithm(
                    "When OpenSSL is older than 1.0.1 then only SHA1 is "
                    "supported with MGF1.",
                    _Reasons.UNSUPPORTED_HASH
                )

            if self._backend._lib.Cryptography_HAS_PKEY_CTX:
                self._finalize_method = self._finalize_pkey_ctx
                self._padding_enum = self._backend._lib.RSA_PKCS1_PSS_PADDING
            else:
                self._finalize_method = self._finalize_pss
        else:
            raise UnsupportedAlgorithm(
                "{0} is not supported by this backend.".format(padding.name),
                _Reasons.UNSUPPORTED_PADDING
            )

        self._padding = padding
        self._algorithm = algorithm
        self._hash_ctx = hashes.Hash(self._algorithm, self._backend)

    def update(self, data):
        self._hash_ctx.update(data)

    def finalize(self):
        evp_md = self._backend._lib.EVP_get_digestbyname(
            self._algorithm.name.encode("ascii"))
        assert evp_md != self._backend._ffi.NULL

        return self._finalize_method(evp_md)

    def _finalize_pkey_ctx(self, evp_md):
        pkey_ctx = self._backend._lib.EVP_PKEY_CTX_new(
            self._private_key._evp_pkey, self._backend._ffi.NULL
        )
        assert pkey_ctx != self._backend._ffi.NULL
        pkey_ctx = self._backend._ffi.gc(pkey_ctx,
                                         self._backend._lib.EVP_PKEY_CTX_free)
        res = self._backend._lib.EVP_PKEY_sign_init(pkey_ctx)
        assert res == 1
        res = self._backend._lib.EVP_PKEY_CTX_set_signature_md(
            pkey_ctx, evp_md)
        assert res > 0

        res = self._backend._lib.EVP_PKEY_CTX_set_rsa_padding(
            pkey_ctx, self._padding_enum)
        assert res > 0
        if isinstance(self._padding, PSS):
            res = self._backend._lib.EVP_PKEY_CTX_set_rsa_pss_saltlen(
                pkey_ctx,
                _get_rsa_pss_salt_length(
                    self._padding,
                    self._private_key.key_size,
                    self._hash_ctx.algorithm.digest_size
                )
            )
            assert res > 0

            if self._backend._lib.Cryptography_HAS_MGF1_MD:
                # MGF1 MD is configurable in OpenSSL 1.0.1+
                mgf1_md = self._backend._lib.EVP_get_digestbyname(
                    self._padding._mgf._algorithm.name.encode("ascii"))
                assert mgf1_md != self._backend._ffi.NULL
                res = self._backend._lib.EVP_PKEY_CTX_set_rsa_mgf1_md(
                    pkey_ctx, mgf1_md
                )
                assert res > 0
        data_to_sign = self._hash_ctx.finalize()
        buflen = self._backend._ffi.new("size_t *")
        res = self._backend._lib.EVP_PKEY_sign(
            pkey_ctx,
            self._backend._ffi.NULL,
            buflen,
            data_to_sign,
            len(data_to_sign)
        )
        assert res == 1
        buf = self._backend._ffi.new("unsigned char[]", buflen[0])
        res = self._backend._lib.EVP_PKEY_sign(
            pkey_ctx, buf, buflen, data_to_sign, len(data_to_sign))
        if res != 1:
            errors = self._backend._consume_errors()
            assert errors[0].lib == self._backend._lib.ERR_LIB_RSA
            reason = None
            if (errors[0].reason ==
                    self._backend._lib.RSA_R_DATA_TOO_LARGE_FOR_KEY_SIZE):
                reason = ("Salt length too long for key size. Try using "
                          "MAX_LENGTH instead.")
            elif (errors[0].reason ==
                    self._backend._lib.RSA_R_DIGEST_TOO_BIG_FOR_RSA_KEY):
                reason = "Digest too large for key size. Use a larger key."
            assert reason is not None
            raise ValueError(reason)

        return self._backend._ffi.buffer(buf)[:]

    def _finalize_pkcs1(self, evp_md):
        if self._hash_ctx._ctx is None:
            raise AlreadyFinalized("Context has already been finalized.")

        sig_buf = self._backend._ffi.new("char[]", self._pkey_size)
        sig_len = self._backend._ffi.new("unsigned int *")
        res = self._backend._lib.EVP_SignFinal(
            self._hash_ctx._ctx._ctx,
            sig_buf,
            sig_len,
            self._private_key._evp_pkey
        )
        self._hash_ctx.finalize()
        if res == 0:
            errors = self._backend._consume_errors()
            assert errors[0].lib == self._backend._lib.ERR_LIB_RSA
            assert (errors[0].reason ==
                    self._backend._lib.RSA_R_DIGEST_TOO_BIG_FOR_RSA_KEY)
            raise ValueError("Digest too large for key size. Use a larger "
                             "key.")

        return self._backend._ffi.buffer(sig_buf)[:sig_len[0]]

    def _finalize_pss(self, evp_md):
        data_to_sign = self._hash_ctx.finalize()
        padded = self._backend._ffi.new("unsigned char[]", self._pkey_size)
        res = self._backend._lib.RSA_padding_add_PKCS1_PSS(
            self._private_key._rsa_cdata,
            padded,
            data_to_sign,
            evp_md,
            _get_rsa_pss_salt_length(
                self._padding,
                self._private_key.key_size,
                len(data_to_sign)
            )
        )
        if res != 1:
            errors = self._backend._consume_errors()
            assert errors[0].lib == self._backend._lib.ERR_LIB_RSA
            assert (errors[0].reason ==
                    self._backend._lib.RSA_R_DATA_TOO_LARGE_FOR_KEY_SIZE)
            raise ValueError("Salt length too long for key size. Try using "
                             "MAX_LENGTH instead.")

        sig_buf = self._backend._ffi.new("char[]", self._pkey_size)
        sig_len = self._backend._lib.RSA_private_encrypt(
            self._pkey_size,
            padded,
            sig_buf,
            self._private_key._rsa_cdata,
            self._backend._lib.RSA_NO_PADDING
        )
        assert sig_len != -1
        return self._backend._ffi.buffer(sig_buf)[:sig_len]


@utils.register_interface(AsymmetricVerificationContext)
class _RSAVerificationContext(object):
    def __init__(self, backend, public_key, signature, padding, algorithm):
        self._backend = backend
        self._public_key = public_key
        self._signature = signature

        if not isinstance(padding, AsymmetricPadding):
            raise TypeError("Expected provider of AsymmetricPadding.")

        self._pkey_size = self._backend._lib.EVP_PKEY_size(
            self._public_key._evp_pkey
        )

        if isinstance(padding, PKCS1v15):
            if self._backend._lib.Cryptography_HAS_PKEY_CTX:
                self._verify_method = self._verify_pkey_ctx
                self._padding_enum = self._backend._lib.RSA_PKCS1_PADDING
            else:
                self._verify_method = self._verify_pkcs1
        elif isinstance(padding, PSS):
            if not isinstance(padding._mgf, MGF1):
                raise UnsupportedAlgorithm(
                    "Only MGF1 is supported by this backend.",
                    _Reasons.UNSUPPORTED_MGF
                )

            # Size of key in bytes - 2 is the maximum
            # PSS signature length (salt length is checked later)
            assert self._pkey_size > 0
            if self._pkey_size - algorithm.digest_size - 2 < 0:
                raise ValueError(
                    "Digest too large for key size. Check that you have the "
                    "correct key and digest algorithm."
                )

            if not self._backend._mgf1_hash_supported(padding._mgf._algorithm):
                raise UnsupportedAlgorithm(
                    "When OpenSSL is older than 1.0.1 then only SHA1 is "
                    "supported with MGF1.",
                    _Reasons.UNSUPPORTED_HASH
                )

            if self._backend._lib.Cryptography_HAS_PKEY_CTX:
                self._verify_method = self._verify_pkey_ctx
                self._padding_enum = self._backend._lib.RSA_PKCS1_PSS_PADDING
            else:
                self._verify_method = self._verify_pss
        else:
            raise UnsupportedAlgorithm(
                "{0} is not supported by this backend.".format(padding.name),
                _Reasons.UNSUPPORTED_PADDING
            )

        self._padding = padding
        self._algorithm = algorithm
        self._hash_ctx = hashes.Hash(self._algorithm, self._backend)

    def update(self, data):
        self._hash_ctx.update(data)

    def verify(self):
        evp_md = self._backend._lib.EVP_get_digestbyname(
            self._algorithm.name.encode("ascii"))
        assert evp_md != self._backend._ffi.NULL

        self._verify_method(evp_md)

    def _verify_pkey_ctx(self, evp_md):
        pkey_ctx = self._backend._lib.EVP_PKEY_CTX_new(
            self._public_key._evp_pkey, self._backend._ffi.NULL
        )
        assert pkey_ctx != self._backend._ffi.NULL
        pkey_ctx = self._backend._ffi.gc(pkey_ctx,
                                         self._backend._lib.EVP_PKEY_CTX_free)
        res = self._backend._lib.EVP_PKEY_verify_init(pkey_ctx)
        assert res == 1
        res = self._backend._lib.EVP_PKEY_CTX_set_signature_md(
            pkey_ctx, evp_md)
        assert res > 0

        res = self._backend._lib.EVP_PKEY_CTX_set_rsa_padding(
            pkey_ctx, self._padding_enum)
        assert res > 0
        if isinstance(self._padding, PSS):
            res = self._backend._lib.EVP_PKEY_CTX_set_rsa_pss_saltlen(
                pkey_ctx,
                _get_rsa_pss_salt_length(
                    self._padding,
                    self._public_key.key_size,
                    self._hash_ctx.algorithm.digest_size
                )
            )
            assert res > 0
            if self._backend._lib.Cryptography_HAS_MGF1_MD:
                # MGF1 MD is configurable in OpenSSL 1.0.1+
                mgf1_md = self._backend._lib.EVP_get_digestbyname(
                    self._padding._mgf._algorithm.name.encode("ascii"))
                assert mgf1_md != self._backend._ffi.NULL
                res = self._backend._lib.EVP_PKEY_CTX_set_rsa_mgf1_md(
                    pkey_ctx, mgf1_md
                )
                assert res > 0

        data_to_verify = self._hash_ctx.finalize()
        res = self._backend._lib.EVP_PKEY_verify(
            pkey_ctx,
            self._signature,
            len(self._signature),
            data_to_verify,
            len(data_to_verify)
        )
        # The previous call can return negative numbers in the event of an
        # error. This is not a signature failure but we need to fail if it
        # occurs.
        assert res >= 0
        if res == 0:
            errors = self._backend._consume_errors()
            assert errors
            raise InvalidSignature

    def _verify_pkcs1(self, evp_md):
        if self._hash_ctx._ctx is None:
            raise AlreadyFinalized("Context has already been finalized.")

        res = self._backend._lib.EVP_VerifyFinal(
            self._hash_ctx._ctx._ctx,
            self._signature,
            len(self._signature),
            self._public_key._evp_pkey
        )
        self._hash_ctx.finalize()
        # The previous call can return negative numbers in the event of an
        # error. This is not a signature failure but we need to fail if it
        # occurs.
        assert res >= 0
        if res == 0:
            errors = self._backend._consume_errors()
            assert errors
            raise InvalidSignature

    def _verify_pss(self, evp_md):
        buf = self._backend._ffi.new("unsigned char[]", self._pkey_size)
        res = self._backend._lib.RSA_public_decrypt(
            len(self._signature),
            self._signature,
            buf,
            self._public_key._rsa_cdata,
            self._backend._lib.RSA_NO_PADDING
        )
        if res != self._pkey_size:
            errors = self._backend._consume_errors()
            assert errors
            raise InvalidSignature

        data_to_verify = self._hash_ctx.finalize()
        res = self._backend._lib.RSA_verify_PKCS1_PSS(
            self._public_key._rsa_cdata,
            data_to_verify,
            evp_md,
            buf,
            _get_rsa_pss_salt_length(
                self._padding,
                self._public_key.key_size,
                len(data_to_verify)
            )
        )
        if res != 1:
            errors = self._backend._consume_errors()
            assert errors
            raise InvalidSignature


@utils.register_interface(RSAPrivateKeyWithNumbers)
@utils.register_interface(RSAPrivateKeyWithSerialization)
class _RSAPrivateKey(object):
    def __init__(self, backend, rsa_cdata):
        self._backend = backend
        self._rsa_cdata = rsa_cdata

        evp_pkey = self._backend._lib.EVP_PKEY_new()
        assert evp_pkey != self._backend._ffi.NULL
        evp_pkey = self._backend._ffi.gc(
            evp_pkey, self._backend._lib.EVP_PKEY_free
        )
        res = self._backend._lib.EVP_PKEY_set1_RSA(evp_pkey, rsa_cdata)
        assert res == 1
        self._evp_pkey = evp_pkey

        self._key_size = self._backend._lib.BN_num_bits(self._rsa_cdata.n)

    key_size = utils.read_only_property("_key_size")

    def signer(self, padding, algorithm):
        return _RSASignatureContext(self._backend, self, padding, algorithm)

    def decrypt(self, ciphertext, padding):
        key_size_bytes = int(math.ceil(self.key_size / 8.0))
        if key_size_bytes != len(ciphertext):
            raise ValueError("Ciphertext length must be equal to key size.")

        return _enc_dec_rsa(self._backend, self, ciphertext, padding)

    def public_key(self):
        ctx = self._backend._lib.RSA_new()
        assert ctx != self._backend._ffi.NULL
        ctx = self._backend._ffi.gc(ctx, self._backend._lib.RSA_free)
        ctx.e = self._backend._lib.BN_dup(self._rsa_cdata.e)
        ctx.n = self._backend._lib.BN_dup(self._rsa_cdata.n)
        res = self._backend._lib.RSA_blinding_on(ctx, self._backend._ffi.NULL)
        assert res == 1
        return _RSAPublicKey(self._backend, ctx)

    def private_numbers(self):
        return rsa.RSAPrivateNumbers(
            p=self._backend._bn_to_int(self._rsa_cdata.p),
            q=self._backend._bn_to_int(self._rsa_cdata.q),
            d=self._backend._bn_to_int(self._rsa_cdata.d),
            dmp1=self._backend._bn_to_int(self._rsa_cdata.dmp1),
            dmq1=self._backend._bn_to_int(self._rsa_cdata.dmq1),
            iqmp=self._backend._bn_to_int(self._rsa_cdata.iqmp),
            public_numbers=rsa.RSAPublicNumbers(
                e=self._backend._bn_to_int(self._rsa_cdata.e),
                n=self._backend._bn_to_int(self._rsa_cdata.n),
            )
        )

    def private_bytes(self, encoding, format, encryption_algorithm):
        return self._backend._private_key_bytes(
            encoding,
            format,
            encryption_algorithm,
            self._backend._lib.PEM_write_bio_RSAPrivateKey,
            self._evp_pkey,
            self._rsa_cdata
        )


@utils.register_interface(RSAPublicKeyWithNumbers)
class _RSAPublicKey(object):
    def __init__(self, backend, rsa_cdata):
        self._backend = backend
        self._rsa_cdata = rsa_cdata

        evp_pkey = self._backend._lib.EVP_PKEY_new()
        assert evp_pkey != self._backend._ffi.NULL
        evp_pkey = self._backend._ffi.gc(
            evp_pkey, self._backend._lib.EVP_PKEY_free
        )
        res = self._backend._lib.EVP_PKEY_set1_RSA(evp_pkey, rsa_cdata)
        assert res == 1
        self._evp_pkey = evp_pkey

        self._key_size = self._backend._lib.BN_num_bits(self._rsa_cdata.n)

    key_size = utils.read_only_property("_key_size")

    def verifier(self, signature, padding, algorithm):
        return _RSAVerificationContext(
            self._backend, self, signature, padding, algorithm
        )

    def encrypt(self, plaintext, padding):
        return _enc_dec_rsa(self._backend, self, plaintext, padding)

    def public_numbers(self):
        return rsa.RSAPublicNumbers(
            e=self._backend._bn_to_int(self._rsa_cdata.e),
            n=self._backend._bn_to_int(self._rsa_cdata.n),
        )
