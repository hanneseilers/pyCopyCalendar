<?php
/**
 * One-time web setup wizard for calendar-sync.
 *
 * WHAT THIS IS: a single self-contained PHP file that lets you enter
 * the Nextcloud connection details through a browser form instead of
 * editing config/config.yaml and secrets/nextcloud.env by hand over
 * SSH/SFTP. It writes both files only after this application's own
 * `--validate-config` and `--preflight` both succeed against what you
 * entered (so a live Nextcloud connection is actually proven to work
 * before anything is kept), and then deletes itself - so it can never
 * be used again to change the configuration afterwards. If it is ever
 * loaded again while config/config.yaml already exists (e.g. because
 * self-deletion failed, or you restored an old copy of this file), it
 * refuses outright and deletes itself again instead of showing the form.
 *
 * BEFORE UPLOADING:
 *   1. Change SETUP_TOKEN below to a long random value, e.g. generate
 *      one with:  php -r "echo bin2hex(random_bytes(24)), PHP_EOL;"
 *   2. Upload the whole project (including this file) to your webspace.
 *      This file must sit where your web server can reach it over
 *      HTTP(S) - the project's root .htaccess denies everything else,
 *      with one explicit exception added for this file's name (see the
 *      "setup.php" block near the top of .htaccess). If you rename this
 *      file, update that .htaccess block to match.
 *   3. Open https://your-domain/path/to/calendar-sync/setup.php?token=<your token>
 *   4. Fill in the form. Nothing is written to disk until the
 *      connection test against Nextcloud actually succeeds.
 *
 * AFTER USE: this file deletes itself on success. If for any reason it
 * is still present afterwards (check via SFTP), delete it by hand -
 * and remove the matching exception block from .htaccess - since a
 * world-reachable PHP file with your Nextcloud app password ever
 * flowing through it is not something to leave lying around longer
 * than necessary.
 */

declare(strict_types=1);

// ============================================================================
const SETUP_TOKEN = 'change-me-to-a-long-random-value';
// ============================================================================

error_reporting(E_ALL);
ini_set('display_errors', '0'); // never leak raw PHP errors/paths to the browser
@set_time_limit(180); // --preflight retries with backoff and can take a while

header('X-Robots-Tag: noindex, nofollow');
header('X-Content-Type-Options: nosniff');
header('Cache-Control: no-store, no-cache, must-revalidate');
header('Referrer-Policy: no-referrer');

$projectRoot = __DIR__;
$configDir   = $projectRoot . '/config';
$secretsDir  = $projectRoot . '/secrets';
$configPath  = $configDir . '/config.yaml';
$secretsPath = $secretsDir . '/nextcloud.env';

function h(string $s): string
{
    return htmlspecialchars($s, ENT_QUOTES, 'UTF-8');
}

function yamlStr($value): string
{
    $value = str_replace(['\\', '"'], ['\\\\', '\\"'], (string) $value);
    return '"' . $value . '"';
}

function selfDestruct(): bool
{
    return @unlink(__FILE__);
}

function pageShell(string $title, string $body): string
{
    return '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        . '<meta name="viewport" content="width=device-width, initial-scale=1">'
        . '<title>' . h($title) . '</title><style>'
        . 'body{font-family:system-ui,sans-serif;max-width:780px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#1a1a1a}'
        . 'fieldset{margin-bottom:1.5rem;border:1px solid #ccc;border-radius:6px;padding:1rem}'
        . 'legend{font-weight:600;padding:0 .4rem}'
        . 'label{display:block;margin-top:.6rem;font-weight:500}'
        . 'input[type=text],input[type=password],input[type=url]{width:100%;box-sizing:border-box;padding:.4rem;margin-top:.2rem}'
        . '.row{display:flex;gap:.5rem}.row>div{flex:1}'
        . '.hint{color:#555;font-size:.85em;margin-top:.15rem}'
        . '.errors{background:#fdecea;border:1px solid #f5c2c0;border-radius:6px;padding:.8rem 1rem;margin-bottom:1rem}'
        . '.errors ul{margin:.3rem 0 0;padding-left:1.2rem}'
        . 'pre{background:#f5f5f5;border:1px solid #ddd;border-radius:6px;padding:.8rem;overflow-x:auto;white-space:pre-wrap;font-size:.85em}'
        . 'button{padding:.6rem 1.4rem;font-size:1rem;cursor:pointer}'
        . '.ok{background:#eaf7ea;border:1px solid #b9e3b9;border-radius:6px;padding:.8rem 1rem}'
        . '</style></head><body>' . $body . '</body></html>';
}

function runPythonCheck(string $projectRoot, string $configPath, array $args): array
{
    if (!function_exists('proc_open')) {
        return [false, "proc_open() ist auf diesem Hosting deaktiviert - der Verbindungstest kann von hier aus nicht ausgeführt werden.\nBitte stattdessen strato_connection_test.sh per SSH ausführen."];
    }
    $pythonBin = is_executable($projectRoot . '/.venv/bin/python')
        ? $projectRoot . '/.venv/bin/python'
        : 'python3';

    $parts = [escapeshellarg($pythonBin), '-m', 'calendar_sync', '--config', escapeshellarg($configPath)];
    foreach ($args as $arg) {
        $parts[] = escapeshellarg($arg);
    }
    $cmd = implode(' ', $parts);

    $env = [
        'PYTHONPATH' => $projectRoot . '/src',
        'PATH' => getenv('PATH') !== false ? getenv('PATH') : '/usr/bin:/bin',
    ];
    $descriptors = [1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
    $proc = @proc_open($cmd, $descriptors, $pipes, $projectRoot, $env);
    if (!is_resource($proc)) {
        return [false, 'Konnte den Python-Prozess nicht starten.'];
    }
    $stdout = stream_get_contents($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $exitCode = proc_close($proc);
    $output = trim($stdout . ($stderr !== '' ? "\n" . $stderr : ''));
    return [$exitCode === 0, $output];
}

// -----------------------------------------------------------------------
// Guard 1: setup already completed. Never show the form again, and clean
// up this file if it's somehow still here.
// -----------------------------------------------------------------------
if (is_file($configPath)) {
    selfDestruct();
    http_response_code(403);
    echo pageShell('Bereits konfiguriert', '<p>Es existiert bereits eine <code>config/config.yaml</code>. '
        . 'Dieses Setup-Skript darf eine bestehende Konfiguration nicht überschreiben und hat sich soeben '
        . '(erneut) selbst gelöscht, falls es noch vorhanden war.</p>'
        . '<p>Konfiguration ändern? Bitte <code>config/config.yaml</code> direkt per SSH/SFTP bearbeiten.</p>');
    exit;
}

// -----------------------------------------------------------------------
// Guard 2: access token. Change SETUP_TOKEN above before uploading.
// -----------------------------------------------------------------------
$providedToken = (string) ($_GET['token'] ?? $_POST['token'] ?? '');
$tokenIsDefault = hash_equals('change-me-to-a-long-random-value', SETUP_TOKEN);
if ($tokenIsDefault || $providedToken === '' || !hash_equals(SETUP_TOKEN, $providedToken)) {
    http_response_code(403);
    if ($tokenIsDefault) {
        echo pageShell('Nicht konfiguriert', '<p>SETUP_TOKEN wurde in setup.php noch nicht geändert. '
            . 'Bitte lokal editieren, neu hochladen, dann erneut aufrufen.</p>');
    } else {
        echo pageShell('Zugriff verweigert', '<p>Zugriff verweigert.</p>');
    }
    exit;
}

// -----------------------------------------------------------------------
// Guard 3: require HTTPS for anything that submits the app password.
// -----------------------------------------------------------------------
$isHttps = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
    || (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https');
if (!$isHttps) {
    http_response_code(400);
    echo pageShell('HTTPS erforderlich', '<p>Bitte über HTTPS aufrufen (nicht http://), '
        . 'da hier gleich ein Passwort übertragen wird.</p>');
    exit;
}

$errors = [];
$testOutput = '';
$success = false;

$values = [
    'base_url'          => '',
    'username'          => '',
    'app_password'      => '',
    'target_url'        => '',
    'timezone'          => 'Europe/Berlin',
    'lookback_days'     => '7',
    'lookahead_days'    => '180',
    'dry_run_default'   => '1',
    'sources'           => [['id' => '', 'url' => ''], ['id' => '', 'url' => ''], ['id' => '', 'url' => '']],
    'locations'         => [['canonical' => '', 'aliases' => ''], ['canonical' => '', 'aliases' => ''], ['canonical' => '', 'aliases' => '']],
];

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $values['base_url']        = trim((string) ($_POST['base_url'] ?? ''));
    $values['username']        = trim((string) ($_POST['username'] ?? ''));
    $values['app_password']    = (string) ($_POST['app_password'] ?? '');
    $values['target_url']      = trim((string) ($_POST['target_url'] ?? ''));
    $values['timezone']        = trim((string) ($_POST['timezone'] ?? '')) ?: 'Europe/Berlin';
    $values['lookback_days']   = trim((string) ($_POST['lookback_days'] ?? '7'));
    $values['lookahead_days']  = trim((string) ($_POST['lookahead_days'] ?? '180'));
    $values['dry_run_default'] = isset($_POST['dry_run_default']) ? '1' : '0';

    $postedSourceIds  = $_POST['source_id'] ?? [];
    $postedSourceUrls = $_POST['source_url'] ?? [];
    $sources = [];
    foreach ($postedSourceIds as $i => $rawId) {
        $id  = trim((string) $rawId);
        $url = trim((string) ($postedSourceUrls[$i] ?? ''));
        $values['sources'][$i] = ['id' => $id, 'url' => $url];
        if ($id !== '' && $url !== '') {
            $sources[] = ['id' => $id, 'url' => $url];
        } elseif ($id !== '' || $url !== '') {
            $errors[] = 'Quellkalender Zeile ' . ($i + 1) . ': bitte ID UND URL angeben (oder beide Felder leer lassen).';
        }
    }

    $postedCanonical = $_POST['loc_canonical'] ?? [];
    $postedAliases   = $_POST['loc_aliases'] ?? [];
    $locations = [];
    foreach ($postedCanonical as $i => $rawCanonical) {
        $canonical  = trim((string) $rawCanonical);
        $aliasesRaw = trim((string) ($postedAliases[$i] ?? ''));
        $values['locations'][$i] = ['canonical' => $canonical, 'aliases' => $aliasesRaw];
        if ($canonical !== '') {
            $aliases = $aliasesRaw !== ''
                ? array_values(array_filter(array_map('trim', explode(',', $aliasesRaw)), fn($a) => $a !== ''))
                : [];
            if (!$aliases) {
                $aliases = [$canonical];
            }
            $locations[] = ['canonical' => $canonical, 'aliases' => $aliases];
        }
    }

    // --- basic sanity checks; the authoritative schema validation is
    // delegated to this app's own `--validate-config` below, so the
    // rules never have to be kept in sync by hand in two places. ---
    if ($values['base_url'] === '' || !preg_match('#^https://#i', $values['base_url'])) {
        $errors[] = 'Nextcloud Base-URL muss mit https:// beginnen.';
    }
    if ($values['username'] === '') {
        $errors[] = 'Nextcloud-Benutzername fehlt.';
    }
    if ($values['app_password'] === '') {
        $errors[] = 'App-Passwort fehlt (nicht das normale Login-Passwort - siehe Nextcloud Einstellungen > Sicherheit).';
    }
    if ($values['target_url'] === '' || !preg_match('#^https://#i', $values['target_url'])) {
        $errors[] = 'Ziel-Kalender-URL fehlt oder beginnt nicht mit https://.';
    }
    if (!$sources) {
        $errors[] = 'Mindestens ein Quellkalender (ID + URL) muss angegeben werden.';
    }
    $sourceIds = array_map(fn($s) => $s['id'], $sources);
    if (count($sourceIds) !== count(array_unique($sourceIds))) {
        $errors[] = 'Quellkalender-IDs müssen eindeutig sein.';
    }
    if (!$locations) {
        $errors[] = 'Mindestens ein Ort (Name) muss angegeben werden.';
    }
    if (!ctype_digit($values['lookback_days']) || !ctype_digit($values['lookahead_days'])) {
        $errors[] = 'Zeitfenster (Rück-/Vorlaufzeit) müssen positive ganze Zahlen (Tage) sein.';
    }
    if (!is_dir($configDir) || !is_dir($secretsDir)) {
        $errors[] = 'config/ oder secrets/ Verzeichnis fehlt - wurde das ganze Projekt korrekt hochgeladen?';
    }

    if (!$errors) {
        $yaml  = "nextcloud:\n";
        $yaml .= '  base_url: ' . yamlStr($values['base_url']) . "\n";
        $yaml .= "  credentials_file: \"secrets/nextcloud.env\"\n";
        $yaml .= "  verify_tls: true\n";
        $yaml .= "  connect_timeout_seconds: 10\n";
        $yaml .= "  read_timeout_seconds: 45\n";
        $yaml .= "  user_agent: \"calendar-sync/1.0\"\n\n";

        $yaml .= "sources:\n";
        foreach ($sources as $s) {
            $yaml .= '  - id: ' . yamlStr($s['id']) . "\n";
            $yaml .= '    calendar_url: ' . yamlStr($s['url']) . "\n";
            $yaml .= "    enabled: true\n";
        }
        $yaml .= "\n";

        $yaml .= "target:\n";
        $yaml .= '  calendar_url: ' . yamlStr($values['target_url']) . "\n\n";

        $yaml .= "location_filter:\n";
        $yaml .= "  match_mode: \"normalized_exact\"\n";
        $yaml .= "  case_sensitive: false\n";
        $yaml .= "  locations:\n";
        foreach ($locations as $l) {
            $yaml .= '    - canonical: ' . yamlStr($l['canonical']) . "\n";
            $yaml .= '      aliases: [' . implode(', ', array_map('yamlStr', $l['aliases'])) . "]\n";
        }
        $yaml .= "\n";

        $yaml .= "window:\n";
        $yaml .= '  timezone: ' . yamlStr($values['timezone']) . "\n";
        $yaml .= '  lookback_days: ' . (int) $values['lookback_days'] . "\n";
        $yaml .= '  lookahead_days: ' . (int) $values['lookahead_days'] . "\n";
        $yaml .= "  outside_window_policy: \"retain\"\n\n";

        $yaml .= "mirroring:\n";
        $yaml .= "  copy_description: true\n";
        $yaml .= "  copy_url: true\n";
        $yaml .= "  copy_categories: true\n";
        $yaml .= "  copy_alarms: false\n";
        $yaml .= "  copy_attendees: false\n";
        $yaml .= "  copy_organizer: false\n";
        $yaml .= "  strip_fields: []\n\n";

        $yaml .= "storage:\n";
        $yaml .= "  database: \"data/sync.sqlite3\"\n";
        $yaml .= "  log_file: \"logs/sync.log\"\n";
        $yaml .= "  lock_file: \"run/calendar-sync.lock\"\n";
        $yaml .= "  backup_directory: \"data/backups\"\n";
        $yaml .= "  backup_retention: 10\n\n";

        $yaml .= "safety:\n";
        $yaml .= '  dry_run_default: ' . ($values['dry_run_default'] === '1' ? 'true' : 'false') . "\n";
        $yaml .= "  max_deletes_absolute: 50\n";
        $yaml .= "  max_delete_ratio: 0.25\n";
        $yaml .= "  max_runtime_seconds: 720\n";
        $yaml .= "  require_all_sources: true\n";
        $yaml .= "  max_expansion_count: 5000\n\n";

        $yaml .= "logging:\n";
        $yaml .= "  level: \"INFO\"\n";
        $yaml .= "  max_bytes: 1000000\n";
        $yaml .= "  backup_count: 5\n";

        $envContent = 'NEXTCLOUD_USERNAME=' . str_replace(["\r", "\n"], '', $values['username']) . "\n"
            . 'NEXTCLOUD_APP_PASSWORD=' . str_replace(["\r", "\n"], '', $values['app_password']) . "\n";

        $writeOk = @file_put_contents($configPath, $yaml) !== false
            && @file_put_contents($secretsPath, $envContent) !== false;
        if ($writeOk) {
            @chmod($configPath, 0600);
            @chmod($secretsPath, 0600);
        } else {
            $errors[] = 'Konnte config/config.yaml oder secrets/nextcloud.env nicht schreiben (Dateirechte prüfen).';
            @unlink($configPath);
            @unlink($secretsPath);
        }
    }

    if (!$errors) {
        [$ok, $out1] = runPythonCheck($projectRoot, $configPath, ['--validate-config']);
        $testOutput = "\$ calendar-sync --validate-config\n" . $out1;
        if ($ok) {
            [$ok, $out2] = runPythonCheck($projectRoot, $configPath, ['--preflight', '--verbose']);
            $testOutput .= "\n\n\$ calendar-sync --preflight --verbose\n" . $out2;
        }
        if (!$ok) {
            $errors[] = 'Verbindungstest fehlgeschlagen - die Konfiguration wurde NICHT übernommen (Ausgabe unten). Bitte Angaben korrigieren und erneut absenden.';
            @unlink($configPath);
            @unlink($secretsPath);
        } else {
            $success = true;
        }
    }
}

if ($success) {
    $deleted = selfDestruct();
    $note = $deleted
        ? '<p>Dieses Setup-Skript (<code>setup.php</code>) hat sich soeben selbst gelöscht.</p>'
        : '<p><strong>Achtung:</strong> Dieses Setup-Skript konnte sich nicht selbst löschen (Dateirechte?). '
          . 'Bitte <code>setup.php</code> jetzt manuell per SFTP löschen und die zugehörige Ausnahme in '
          . '<code>.htaccess</code> entfernen.</p>';
    echo pageShell('Setup abgeschlossen', '<div class="ok"><h1>✓ Konfiguration gespeichert</h1>'
        . '<p>Verbindungstest erfolgreich - <code>config/config.yaml</code> und '
        . '<code>secrets/nextcloud.env</code> wurden geschrieben.</p></div>'
        . $note
        . '<p>Nächster Schritt: einen Cronjob einrichten, der periodisch aufruft:</p>'
        . '<pre>' . h($projectRoot) . "/.venv/bin/python -m calendar_sync --config config/config.yaml --apply</pre>"
        . '<p>(<code>safety.dry_run_default</code> steht aktuell auf <strong>'
        . ($values['dry_run_default'] === '1' ? 'true (dry-run)' : 'false (wendet änderungen sofort an)')
        . '</strong> - siehe README, Abschnitt "Dry run" / "First real run".)</p>'
        . '<h2>Testausgabe</h2><pre>' . h($testOutput) . '</pre>');
    exit;
}

// -----------------------------------------------------------------------
// Render the form (initial load, or re-shown after validation/test errors).
// -----------------------------------------------------------------------
$errorsHtml = '';
if ($errors) {
    $errorsHtml = '<div class="errors"><strong>Bitte korrigieren:</strong><ul>';
    foreach ($errors as $e) {
        $errorsHtml .= '<li>' . h($e) . '</li>';
    }
    $errorsHtml .= '</ul></div>';
}
if ($testOutput !== '') {
    $errorsHtml .= '<h3>Testausgabe des letzten Versuchs</h3><pre>' . h($testOutput) . '</pre>';
}

$sourceRows = '';
foreach ($values['sources'] as $i => $s) {
    $sourceRows .= '<div class="row"><div><label>ID</label>'
        . '<input type="text" name="source_id[]" value="' . h($s['id']) . '" placeholder="z.B. dept-a"></div>'
        . '<div><label>Kalender-URL</label>'
        . '<input type="url" name="source_url[]" value="' . h($s['url']) . '" placeholder="https://.../remote.php/dav/calendars/user/quelle/"></div></div>';
}

$locationRows = '';
foreach ($values['locations'] as $i => $l) {
    $locationRows .= '<div class="row"><div><label>Ort (Name)</label>'
        . '<input type="text" name="loc_canonical[]" value="' . h($l['canonical']) . '" placeholder="z.B. Berlin Office"></div>'
        . '<div><label>Weitere Schreibweisen (Komma-getrennt, optional)</label>'
        . '<input type="text" name="loc_aliases[]" value="' . h($l['aliases']) . '" placeholder="Office Berlin, BER Room"></div></div>';
}

$checked = $values['dry_run_default'] === '1' ? ' checked' : '';

$baseUrlEsc       = h($values['base_url']);
$usernameEsc      = h($values['username']);
$targetUrlEsc     = h($values['target_url']);
$timezoneEsc      = h($values['timezone']);
$lookbackEsc      = h($values['lookback_days']);
$lookaheadEsc     = h($values['lookahead_days']);
$providedTokenEsc = h($providedToken);

$form = <<<HTML
<h1>calendar-sync einrichten</h1>
<p>Trägt die Nextcloud-Zugangsdaten und die Kalender-Konfiguration ein.
Geschrieben wird erst, nachdem eine echte Verbindung zu Nextcloud damit
getestet und bestätigt wurde. Dieses Skript löscht sich danach selbst.</p>
{$errorsHtml}
<form method="post">
<input type="hidden" name="token" value="{$providedTokenEsc}">

<fieldset>
<legend>Nextcloud-Zugang</legend>
<label>Base-URL</label>
<input type="url" name="base_url" value="{$baseUrlEsc}" placeholder="https://cloud.example.invalid/remote.php/dav/" required>
<div class="hint">CalDAV-Wurzel deiner Nextcloud, üblicherweise https://DOMAIN/remote.php/dav/</div>
<label>Benutzername</label>
<input type="text" name="username" value="{$usernameEsc}" required>
<label>App-Passwort</label>
<input type="password" name="app_password" value="" required>
<div class="hint">Nicht das normale Login-Passwort - Nextcloud Weboberfläche → Einstellungen → Sicherheit → "Neues App-Passwort erstellen".</div>
</fieldset>

<fieldset>
<legend>Quellkalender (bis zu 3, leere Zeilen werden ignoriert)</legend>
{$sourceRows}
</fieldset>

<fieldset>
<legend>Zielkalender</legend>
<label>Kalender-URL (muss vorher in Nextcloud existieren)</label>
<input type="url" name="target_url" value="{$targetUrlEsc}" placeholder="https://.../remote.php/dav/calendars/user/ziel/" required>
</fieldset>

<fieldset>
<legend>Orte (bis zu 3, leere Zeilen werden ignoriert)</legend>
{$locationRows}
</fieldset>

<fieldset>
<legend>Weitere Einstellungen</legend>
<div class="row">
<div><label>Zeitzone</label><input type="text" name="timezone" value="{$timezoneEsc}"></div>
<div><label>Rücklauf (Tage)</label><input type="text" name="lookback_days" value="{$lookbackEsc}"></div>
<div><label>Vorlauf (Tage)</label><input type="text" name="lookahead_days" value="{$lookaheadEsc}"></div>
</div>
<label><input type="checkbox" name="dry_run_default" value="1"{$checked}> Dry-run als Standard (empfohlen - erst mit <code>--apply</code> werden wirklich Änderungen geschrieben)</label>
</fieldset>

<button type="submit">Speichern &amp; Verbindung testen</button>
</form>
HTML;

echo pageShell('calendar-sync Setup', $form);
