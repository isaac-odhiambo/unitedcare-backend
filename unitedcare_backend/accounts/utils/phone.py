def normalize_kenyan_phone(phone: str) -> str:
    """
    Converts Kenyan phone numbers to international format
    0712345678 -> +254712345678
    0112345678 -> +254112345678
    """
    phone = phone.strip()

    if phone.startswith("0"):
        return "+254" + phone[1:]

    if phone.startswith("+254"):
        return phone

    return phone
