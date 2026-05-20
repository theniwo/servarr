import sqlite3
import mysql.connector
import os
import sys

# Tabellen, die deine echten Einstellungen enthalten
SETTINGS_TABLES = [
    "Indexer", "DownloadClients", "QualityProfiles", "CustomFormats",
    "NamingConfig", "Settings", "RootFolders", "Tags", "Users"
]

def migrate():
    print("--- Starte isolierte Einstellungs-Rettung im Container ---")

    if not os.path.exists('radarr.db'):
        print("[FEHLER] Keine 'radarr.db' im aktuellen Verzeichnis gefunden!")
        sys.exit(1)

    # Variablen aus der Umgebung laden (gemäß MYSQL_ Standard)
    db_host = os.getenv("MYSQL_HOST", "radarr-db")
    db_port = int(os.getenv("MYSQL_PORT", "3306"))
    db_user = os.getenv("MYSQL_USER", "radarr-user")
    db_pass = os.getenv("MYSQL_PASSWORD")
    db_name = os.getenv("MYSQL_DATABASE", "radarr-main")

    if not db_pass:
        print("[FEHLER] Keine 'MYSQL_PASSWORD' Umgebungsvariable übergeben!")
        sys.exit(1)

    print("[SQLITE] Verbinde mit kaputter radarr.db...")
    src = sqlite3.connect('radarr.db')
    src_cursor = src.cursor()

    print(f"[MARIADB] Verbinde mit MariaDB ({db_host}:{db_port}/{db_name})...")
    try:
        dst = mysql.connector.connect(
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_pass,
            database=db_name
        )
        dst_cursor = dst.cursor()
    except Exception as e:
        print(f"[FEHLER] Verbindung zu MariaDB fehlgeschlagen: {e}")
        sys.exit(1)

    dst_cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")

    for table in SETTINGS_TABLES:
        print(f"[MIGRATION] Kopiere Tabelle '{table}'... ", end="", flush=True)
        try:
            src_cursor.execute(f"SELECT * FROM [{table}];")
            rows = src_cursor.fetchall()

            if not rows:
                print("Leer.")
                continue

            dst_cursor.execute(f"TRUNCATE TABLE `{table}`;")
            placeholders = ",".join(["%s"] * len(rows[0]))

            query = f"INSERT INTO `{table}` VALUES ({placeholders});"
            dst_cursor.executemany(query, rows)
            print(f"{len(rows)} Einträge erfolgreich kopiert.")

        except Exception as e:
            print(f"FEHLER: {e}")

    dst_cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
    dst.commit()
    src.close()
    dst.close()
    print("--- Migration beendet! ---")

if __name__ == '__main__':
    migrate()
