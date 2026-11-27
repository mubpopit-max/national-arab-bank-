import hashlib
import json
from datetime import datetime, timezone


# ============================================================
# VORTEX BLOCKCHAIN
# ============================================================

BLOCK_TIME_TARGET_SECONDS = 600  # 10 دقائق

# صعوبة أولية بسيطة للتطوير والاختبار.
# سيتم تطوير آلية الصعوبة لاحقًا للشبكة الفعلية.
INITIAL_DIFFICULTY = 4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def calculate_hash(data):
    """
    إنشاء SHA-256 hash ثابت للبيانات.
    """
    if isinstance(data, dict):
        data = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
        )

    return hashlib.sha256(
        str(data).encode("utf-8")
    ).hexdigest()


def calculate_merkle_root(transactions):
    """
    حساب Merkle Root لمعاملات الكتلة.
    """

    if not transactions:
        return calculate_hash("")

    hashes = []

    for tx in transactions:
        if isinstance(tx, dict):
            tx_data = json.dumps(
                tx,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            tx_data = str(tx)

        hashes.append(calculate_hash(tx_data))

    while len(hashes) > 1:

        if len(hashes) % 2 != 0:
            hashes.append(hashes[-1])

        new_level = []

        for i in range(0, len(hashes), 2):
            combined = hashes[i] + hashes[i + 1]

            new_level.append(
                calculate_hash(combined)
            )

        hashes = new_level

    return hashes[0]


def calculate_block_hash(
    block_height,
    previous_hash,
    merkle_root,
    timestamp,
    nonce,
    difficulty,
):
    """
    حساب hash الكتلة.
    """

    block_data = {
        "block_height": block_height,
        "previous_hash": previous_hash,
        "merkle_root": merkle_root,
        "timestamp": timestamp,
        "nonce": nonce,
        "difficulty": difficulty,
    }

    return calculate_hash(block_data)


def mine_block(
    block_height,
    previous_hash,
    transactions,
    difficulty=INITIAL_DIFFICULTY,
):
    """
    تعدين كتلة باستخدام Proof of Work.

    الهدف:
    إيجاد nonce يجعل hash يبدأ بعدد معين من الأصفار.
    """

    timestamp = utc_now()

    merkle_root = calculate_merkle_root(
        transactions
    )

    nonce = 0

    target_prefix = "0" * difficulty

    while True:

        block_hash = calculate_block_hash(
            block_height=block_height,
            previous_hash=previous_hash,
            merkle_root=merkle_root,
            timestamp=timestamp,
            nonce=nonce,
            difficulty=difficulty,
        )

        if block_hash.startswith(target_prefix):
            return {
                "block_height": block_height,
                "block_hash": block_hash,
                "previous_hash": previous_hash,
                "merkle_root": merkle_root,
                "nonce": nonce,
                "difficulty": difficulty,
                "timestamp": timestamp,
                "transactions": transactions,
            }

        nonce += 1


def verify_block(block):
    """
    التحقق من صحة كتلة VORTEX.
    """

    required_fields = [
        "block_height",
        "block_hash",
        "previous_hash",
        "merkle_root",
        "nonce",
        "difficulty",
        "timestamp",
        "transactions",
    ]

    for field in required_fields:
        if field not in block:
            return False

    calculated_merkle = calculate_merkle_root(
        block["transactions"]
    )

    if calculated_merkle != block["merkle_root"]:
        return False

    calculated_hash = calculate_block_hash(
        block_height=block["block_height"],
        previous_hash=block["previous_hash"],
        merkle_root=block["merkle_root"],
        timestamp=block["timestamp"],
        nonce=block["nonce"],
        difficulty=block["difficulty"],
    )

    if calculated_hash != block["block_hash"]:
        return False

    target_prefix = "0" * int(block["difficulty"])

    if not block["block_hash"].startswith(
        target_prefix
    ):
        return False

    return True


def verify_chain(blocks):
    """
    التحقق من سلسلة VORTEX كاملة.
    """

    if not blocks:
        return True

    for index, block in enumerate(blocks):

        if not verify_block(block):
            return False

        if index > 0:

            previous_block = blocks[index - 1]

            if (
                block["previous_hash"]
                != previous_block["block_hash"]
            ):
                return False

            if (
                block["block_height"]
                != previous_block["block_height"] + 1
            ):
                return False

    return True
