from __future__ import annotations

from src.tiktok.signing import generate_signature


def test_official_get_authorized_shops_signature_example() -> None:
    signature = generate_signature(
        url="https://open-api.tiktokglobalshop.com/authorization/202309/shops",
        query={"app_key": "29a39d", "timestamp": 1623812664},
        app_secret="e59af819cc",
    )
    assert (
        signature
        == "b596b73e0cc6de07ac26f036364178ab16b0a907af13d43f0a0cd2345f582dc8"
    )


def test_sign_excludes_sign_and_access_token() -> None:
    baseline = generate_signature(
        url="https://open-api.tiktokglobalshop.com/authorization/202309/shops",
        query={"app_key": "29a39d", "timestamp": 1623812664},
        app_secret="e59af819cc",
    )
    with_excluded = generate_signature(
        url="https://open-api.tiktokglobalshop.com/authorization/202309/shops",
        query={
            "app_key": "29a39d",
            "timestamp": 1623812664,
            "sign": "ignored",
            "access_token": "ignored",
        },
        app_secret="e59af819cc",
    )
    assert with_excluded == baseline

