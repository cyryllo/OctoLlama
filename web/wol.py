"""Wake-on-LAN — budzenie wyłączonego hosta z panelu WWW.

Zwykły UDP broadcast (magic packet), biblioteka standardowa - zero roota,
w przeciwieństwie do wyłączania/uśpienia (patrz hosts_store.ustaw_zasilanie),
bo to nie wymaga UPRAWNIEŃ na docelowej maszynie - budzi się fizycznie z karty
sieciowej, zanim system w ogóle wystartuje.
"""

import re
import socket

WZORZEC_MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def wyslij_magic_packet(mac, port=9):
    # WHAT: magic packet = 6 bajtów 0xFF + 16x powtórzony adres MAC.
    surowy = mac.replace(":", "").replace("-", "")
    adres = bytes.fromhex(surowy)
    pakiet = b"\xff" * 6 + adres * 16

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pakiet, ("255.255.255.255", port))
    finally:
        s.close()


def _dotknij_hosta(ip, port=11434, timeout=1):
    # WHY: żeby jądro miało w ogóle powód zapytać ARP o ten adres - samo
    # czytanie /proc/net/arp nic nie wywoła, jeśli nigdy nie było ruchu do
    # tego IP. Nie musi się połączyć (host może odrzucić na innym porcie) -
    # sama próba TCP SYN do lokalnego IP wystarczy, żeby ARP się rozwiązał.
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            pass
    except OSError:
        pass


def znajdz_mac(ip):
    # WHAT: czyta tablicę ARP jądra (/proc/net/arp, Linux, do odczytu bez
    # roota) - działa TYLKO gdy host jest teraz włączony i odpowiedział
    # chociaż raz w tej samej sieci L2 (na tym polega ARP). Zwraca None,
    # jeśli host jest offline albo w innej podsieci - wtedy user musi wpisać
    # MAC ręcznie.
    _dotknij_hosta(ip)
    try:
        with open("/proc/net/arp") as f:
            next(f)  # nagłówek
            for linia in f:
                pola = linia.split()
                if len(pola) >= 4 and pola[0] == ip and pola[3] != "00:00:00:00:00:00":
                    return pola[3].upper()
    except OSError:
        pass
    return None
