# ![ikona](icon.png) Pobierz dane GUGiK

Wtyczka QGIS do pobierania danych z usług Głównego Urzędu Geodezji i Kartografii: działek i budynków z EGiB, cen transakcyjnych z RCN, granic i adresów z PRG, nazw geograficznych z PRNG, plików ortofotomapy, LIDAR i NMT ze skorowidzów oraz danych z powiatowych usług WFS zgłoszonych do EZiUDP.

Wtyczka prowadzi przez trzy kroki – **co pobrać → skąd → jak zapisać** – i pobiera dane w tle, więc QGIS nie zamarza nawet przy dużych zakresach.

*English: a QGIS plugin for downloading data from the Polish Head Office of Geodesy and Cartography (GUGiK) – cadastral parcels and buildings, real estate prices, administrative boundaries, addresses, place names, orthophoto/LIDAR/DEM sheets and county WFS services – through a three-step panel and Processing algorithms.*

<p align="center"><img src="docs/img/panel.png" alt="Panel wtyczki: wybór danych, zakresu i sposobu zapisu" width="400"></p>

## Wymagania

- QGIS **3.40 LTR** lub nowszy, w tym **QGIS 4** (Qt6)
- dostęp do internetu; serwer proxy ustawiony w QGIS (*Ustawienia → Opcje → Sieć*) jest używany automatycznie
- interfejs po polsku albo po angielsku – według języka QGIS (każdy język inny niż polski dostaje wersję angielską)

## Instalacja

*Wtyczki → Zarządzanie wtyczkami → Wszystkie*, wyszukaj **Pobierz dane GUGiK** i kliknij *Zainstaluj*. Panel otwiera się przyciskiem na pasku narzędzi albo z menu *Wtyczki → Pobierz dane GUGiK*.

## Jak pobrać dane

### 1. Co pobrać

Zaznacz warstwy na liście albo wpisz fragment nazwy w polu wyszukiwania.

| Grupa | Dane |
|---|---|
| EGiB | działki, budynki (usługa zbiorcza) |
| RCN | działki, budynki i lokale z cenami transakcyjnymi |
| PRG | punkty adresowe, ulice, place, granice jednostek administracyjnych i ewidencyjnych, granice morskie |
| PRNG | urzędowe i pozostałe nazwy miejscowości oraz obiektów fizjograficznych |
| Skorowidze | ortofotomapa, prawdziwa ortofotomapa, LIDAR, NMT i NMPT (KRON86 i EVRF2007) |
| Usługi powiatowe (EZiUDP) | osnowa (BDSOG), EGiB z punktami granicznymi, użytkami i konturami, RCN, BDOT500 – prosto z usług WFS powiatów |

### 2. Skąd

| Zakładka | Zakres |
|---|---|
| **Jednostka** | województwo, powiat, gmina albo obręb – z list albo przez wpisanie kodu TERYT lub nazwy. Lista obrębów gminy pochodzi z ULDK, więc jest aktualna. |
| **Mapa** | bieżący widok, narysowany prostokąt albo dowolny wielokąt (lewy klik dodaje wierzchołek, prawy klik / dwuklik / Enter kończy, Backspace cofa, Esc anuluje) |
| **Warstwa** | obiekty warstwy poligonowej (lub tylko zaznaczone); wynik jest przycinany dokładnie do ich kształtu |
| **ID** | lista identyfikatorów, np. działek `146501_1.0001.23`; działają symbole `*` i `?` |
| **Obręb** | nazwa albo kod obrębu i numery działek |

Wybrany zakres podświetla się na mapie, zanim cokolwiek zostanie pobrane. Kod TERYT spoza słownika wtyczki (np. nowy obręb) też działa – sprawdza go granica z PRG.

### 3. Jak zapisać

- **Warstwy tymczasowe** albo **plik GeoPackage** – kolejne pobrania dopisują się do warstw bez powtórzeń.
- **Prefiks nazw**, np. `ProjektA` – warstwy `ProjektA_Dzialki`, `ProjektA_Budynki` w podgrupie *GUGiK/ProjektA*. Wygodne, gdy zbierasz dane do kilku zadań naraz.
- **Każde pobranie w osobnej warstwie** – zamiast dopisywania, np. `Dzialki_1465_1`, `Dzialki_1465_2`.
- **Domyślny styl** – warstwy dostają gotowe style QML.
- Dla działek EGiB można wybrać tylko potrzebne pola.

Wyniki trafiają do grupy **GUGiK** w drzewie warstw. W *Właściwościach warstwy → Metadane* zapisany jest adres usługi, zakres i historia pobrań z datami.

Kroki zwijają się kliknięciem w nagłówek. Zwinięty krok pokazuje skrót wyboru.

### Zadania

Pod przyciskiem **Pobierz** widać postęp każdego zadania i liczbę pobranych obiektów. Obiekty pojawiają się na mapie już w trakcie pobierania (porcjami co kilka sekund). Zadanie można anulować – to, co już pobrano, zostaje w warstwie. Gdy część zapytań się nie powiedzie, przycisk **Ponów** powtarza tylko je. Komunikaty błędów mówią, co się stało i co zrobić (limit czasu, błąd serwera, proxy, certyfikat). Szczegóły są w *Dzienniku komunikatów*, zakładka **PD_GUGiK**.

## Skorowidze: ortofotomapa, LIDAR, NMT

<p align="center"><img src="docs/img/skorowidz.png" alt="Okno wyboru arkuszy skorowidza" width="640"></p>

Po wyszukaniu arkuszy w zakresie wszystkie zasięgi pojawiają się na mapie jako warstwa, a wybrane są podświetlone. Arkusze wybierasz:

- kliknięciem na mapie (**Wybieraj na mapie**), w tabeli albo zwykłymi narzędziami zaznaczania QGIS,
- filtrem rocznika, typu (np. RGB/CIR) i godła,
- przyciskiem **Najnowsze**, który dla każdego godła zostawia najnowszy rocznik.

Pod tabelą widać łączny rozmiar wybranych plików. Pliki już obecne w folderze docelowym są pomijane i tylko wczytywane na mapę.

## Powiatowe usługi WFS (EZiUDP)

Grupa *Usługi powiatowe (EZiUDP)* w kroku 1 pobiera dane z usług WFS zgłoszonych przez powiaty do Ewidencji Zbiorów i Usług Danych Przestrzennych: osnowę poziomą i wysokościową (BDSOG), EGiB (działki, budynki, punkty graniczne oraz użytki gruntowe i kontury klasyfikacyjne – jeśli powiat je udostępnia), RCN i BDOT500. Pola zostają takie, jak podaje powiat.

Działa to tak samo jak dla usług zbiorczych: zakres wybierasz w kroku 2 (jednostka, mapa albo warstwa – wtyczka sama ustala, których powiatów dotyczy), zapis w kroku 3, a postęp widać na liście zadań. Każdy powiat trafia do osobnej warstwy, np. `Dzialki_1465`. Gdy usługa któregoś powiatu nie odpowie, **Ponów** powtarza tylko ten powiat.

## Algorytmy Processing

W *Przyborniku Processing → Pobierz dane GUGiK* są trzy algorytmy do modeli, trybu wsadowego i skryptów:

- **Pobierz dla jednostki (TERYT)**
- **Pobierz dla zasięgu warstwy**
- **Pobierz według listy identyfikatorów** (z tekstu albo z pola tabeli)

Z wiersza poleceń:

```
qgis_process run pobierz_dane_gugik:pobierz_id -- SOURCE=0 IDS="146501_1.0001.23" OUTPUT=dzialki.gpkg
```

## Dla programistów

```
core/        katalog źródeł, filtry FES, klient WFS, parser GML, zapis warstw, zadania w tle, ULDK
ui/          panel (dock.py), kroki i zadania (widgets.py), skorowidze (sheets.py), styl Qt (style.qss)
processing/  dostawca i algorytmy Processing
data/        style QML i słownik jednostek (jednostki.json.gz)
tests/       testy pytest z udawanymi usługami WFS, PRG i ULDK
i18n/        tłumaczenie angielskie (.ts i .qm)
tools/       budowanie paczki, odświeżanie słownika, tłumaczenia, zapis próbek z usług
```

**Testy** działają na prawdziwym QGIS bez okna, z udawanymi usługami (bez sieci):

```
QT_QPA_PLATFORM=offscreen python -m pytest tests
```

Środowiska QGIS 3.40 i 4.x najprościej postawić z conda-forge, np. `micromamba create -n q4 -c conda-forge qgis pytest requests`.

**Przed wydaniem:**

1. Uruchom `tools/odswiez_slowniki.py` (w konsoli Pythona QGIS albo z wiersza poleceń), żeby odświeżyć słownik województw, powiatów, gmin i obrębów z PRG. Skrypt wypisze zmiany do changelogu.
2. Opcjonalnie `tools/zapisz_probki.py` – zapisuje prawdziwe odpowiedzi usług do `tests/fixtures/live/`, na których testy sprawdzają parser.
3. `python tools/tlumaczenia.py` – zbiera napisy z kodu do `i18n/PD_GUGiK_en.ts` i buduje `.qm`; wypisze napisy bez angielskiego odpowiednika (uzupełnia się je w pliku `.ts`). Test pilnuje, żeby tłumaczenie było kompletne.
4. `python tools/build_zip.py` – paczka do repozytorium QGIS w `dist/` (bez testów i narzędzi).

Paczka przechodzi kontrole repozytorium QGIS: Bandit, detect-secrets, skaner zgodności z Qt6.

Błędy i propozycje: [zgłoszenia na GitHubie](https://github.com/GlownyUrzadGeodezjiIKartografii/pobierz_dane_GUGiK/issues).

## Historia zmian

Pełna lista zmian każdej wersji jest w [metadata.txt](metadata.txt) i na stronie wtyczki w repozytorium QGIS.

### 1.9.x – nowa wtyczka, droga do 2.0

- Nowy panel w trzech krokach, wszystkie pobierania w tle z możliwością anulowania, jeden parser GML zamiast kilku.
- Zakres z mapy: prostokąt i dowolny wielokąt; obiekty wieloczęściowe w zakresie warstwy.
- Grupa GUGiK, metadane warstw, prefiks nazw, osobne warstwy dla każdego pobrania, ponawianie nieudanych zapytań.
- Skorowidze na mapie z wyborem kliknięciem, filtrami i rozmiarem plików.
- EZiUDP przez API JSON, użytki i kontury EGiB, odporność na niepoprawne odpowiedzi usług powiatowych.
- Algorytmy Processing.
- Słownik jednostek w jednym lekkim pliku; aktualna lista obrębów z ULDK.
- Usługi powiatowe w tych samych trzech krokach i na liście zadań; pobieranie kaflami bez luk.
- Interfejs po angielsku.
- Minimalna wersja QGIS 3.40, pełna zgodność z QGIS 4.

### 1.3.0

Zgodność z QGIS 4 (Qt6/PyQt6) przy zachowaniu działania w QGIS 3 (Qt5).

### 1.2

Pobieranie PRG (adresy, ulice, place), PRNG, plików ze skorowidzów (ortofotomapa, prawdziwa ortofotomapa, LIDAR, NMT, NMPT) i danych z powiatowych usług WFS z EZiUDP; poprawione pobieranie dla warstw poligonowych i style QML.

### 1.1

Budynki (EGiB), działki, budynki i lokale (RCN), paczki OPENDATA dla całej Polski i województw, wybór między OPENDATA a WFS dla powiatu, poprawiona obsługa enklaw w PRG.

### 1.0

Pierwsze publiczne wydanie: pobieranie działek dla jednostki administracyjnej, listy identyfikatorów, zasięgu na mapie, warstwy poligonowej, widoku mapy oraz obrębu i numeru działki.

---

[![GUGiK © 2026](https://www.geoportal.gov.pl/wp-content/themes/geoportal/assets/images/gugik-logo.png)](https://www.gov.pl/web/gugik)

[GUGiK 2026](https://www.gov.pl/web/gugik)
