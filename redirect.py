import requests
from tqdm import tqdm
import concurrent.futures
import time
import csv  # for quoting constants
import argparse
from pathlib import Path

# --- CONFIGURATION ---
INPUT_FILE = "Veille Renault - Résultats.csv"
OUTPUT_FILE = "Veille Renault - Résultats.csv_Resolved.csv"
TIMEOUT_SECONDS = 5  # Temps max pour attendre une réponse
MAX_WORKERS = 20     # Nombre de requêtes simultanées (augmenter pour aller plus vite, attention au rate limiting)

# --- FONCTIONS ---
    
def resolve_single_url(original_url):
    """
    Tente de résoudre l'URL finale.
    Retourne (url_originale, url_finale, statut)
    """
    # Si l'URL est vide ou n'est pas une chaîne, on passe
    if original_url is None or not isinstance(original_url, str) or not original_url.strip():
        return original_url, original_url, "EMPTY"

    try:
        response = requests.get(original_url, timeout=TIMEOUT_SECONDS, stream=True, allow_redirects=True)
        final_url = response.url
        
        status = "RESOLVED" if final_url != original_url else "UNCHANGED"
        return original_url, final_url, status

    except requests.Timeout:
        return original_url, original_url, "TIMEOUT"
    except requests.ConnectionError:
        return original_url, original_url, "CONNECTION_ERROR"
    except Exception as e:
        return original_url, original_url, "ERROR"

def main():
    parser = argparse.ArgumentParser(description="Resolve redirects in a CSV URL column.")
    parser.add_argument("--input", default=INPUT_FILE, help=f"Input CSV path (default: {INPUT_FILE})")
    parser.add_argument("--output", default=OUTPUT_FILE, help=f"Output CSV path (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    input_file = args.input
    output_file = args.output

    print(f"Chargement du fichier : {input_file}...", flush=True)
    
    try:
        # Détection du séparateur et de la présence d'un header via csv.Sniffer
        with open(input_file, "r", encoding="utf-8", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=";,")
                dialect.doublequote = True  # sniffer often gets this wrong; multiline fields need it
            except Exception:
                # Fallback: most of your veille exports are comma-separated
                dialect = csv.excel
                dialect.delimiter = ","
            try:
                has_header = csv.Sniffer().has_header(sample)
            except Exception:
                # Fallback: assume header exists if it looks like one
                first_line = sample.splitlines()[0] if sample else ""
                has_header = "URL" in first_line and "Modèle" in first_line
        print(f"CSV détecté: delim='{getattr(dialect, 'delimiter', ',')}', header={'yes' if has_header else 'no'}", flush=True)
    except Exception as e:
        print(f"Erreur lors de l'ouverture du CSV : {e}")
        return

    # Chargement des lignes + extraction des URLs
    rows = []
    urls = []
    with open(input_file, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, dialect=dialect)
        header = None
        if has_header:
            header = next(reader, None)
            if header is None:
                print("Erreur : fichier CSV vide.")
                return
        else:
            # Fallback colonnes attendues si pas de header
            header = ["Date", "Réponse", "Modèle", "Prompt", "Langue", "URL", "Domaine"]

        if "URL" not in header:
            print("Erreur : La colonne 'URL' est introuvable dans le CSV.")
            print("Colonnes disponibles :", header)
            return
        url_idx = header.index("URL")

        for r in reader:
            if not r:
                continue
            # pad/truncate to header length for consistent writing
            if len(r) < len(header):
                r = r + [""] * (len(header) - len(r))
            elif len(r) > len(header):
                r = r[: len(header)]
            rows.append(r)
            urls.append(r[url_idx])

    print(f"Fichier chargé. {len(rows)} lignes trouvées.", flush=True)
    resolved_urls_map = {}
    
    print(f"Démarrage de la résolution des liens avec {MAX_WORKERS} threads...", flush=True)

    # Utilisation de ThreadPoolExecutor pour paralléliser les requêtes
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Lancement des tâches
        future_to_url = {executor.submit(resolve_single_url, url): url for url in urls}
        
        results = []
        for future in tqdm(concurrent.futures.as_completed(future_to_url), total=len(urls), unit="url"):
            res = future.result()
            results.append(res)
            resolved_urls_map[res[0]] = res[1]

    # Analyse des résultats
    success_count = sum(1 for r in results if r[2] == "RESOLVED")
    unchanged_count = sum(1 for r in results if r[2] == "UNCHANGED" or r[2] == "SKIPPED")
    error_count = sum(1 for r in results if r[2] not in ["RESOLVED", "UNCHANGED", "SKIPPED"])

    print("\n--- Résumé ---", flush=True)
    print(f"Liens redirigés avec succès : {success_count}", flush=True)
    print(f"Liens inchangés (directs)   : {unchanged_count}", flush=True)
    print(f"Erreurs / Timeouts          : {error_count}", flush=True)

    # Afficher quelques exemples pour vérification
    print("\nExemples de redirections :", flush=True)
    shown = 0
    for orig, final, status in results:
        if status == "RESOLVED":
            print(f"{orig} -> {final}")
            shown += 1
            if shown >= 10:
                break

    # Mise à jour du DataFrame
    print("Mise à jour des données...", flush=True)
    # On applique le mapping. Si une URL n'est pas dans la map, on garde l'originale
    # On ajoute aussi une colonne URL_Originale
    out_header = list(header)
    if "URL_Originale" not in out_header:
        out_header.append("URL_Originale")
    out_url_idx = out_header.index("URL")
    out_orig_idx = out_header.index("URL_Originale")

    # Sauvegarde
    print(f"Sauvegarde dans {output_file}...", flush=True)
    with open(output_file, "w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, delimiter=getattr(dialect, "delimiter", ","), quoting=csv.QUOTE_MINIMAL)
        writer.writerow(out_header)
        for r in rows:
            out_row = list(r)
            # ensure output length
            if len(out_row) < len(out_header):
                out_row = out_row + [""] * (len(out_header) - len(out_row))
            url_val = out_row[out_url_idx]
            out_row[out_orig_idx] = url_val
            out_row[out_url_idx] = resolved_urls_map.get(url_val, url_val)
            writer.writerow(out_row)
    print("Terminé !", flush=True)

if __name__ == "__main__":
    main()