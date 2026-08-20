/**
 * kd-nifi-auto-fix.js
 *
 * Client-side helper for the KD Configuration Dashboard.
 * - Guards concurrent native file/folder dialogs.
 * - Triggers automatic NiFi NAR extraction + copy when ZipPath /
 *   SetupPath / BasePath become available (POST /api/auto-sync-nifi).
 * - Renders a collapsed-by-default "Details" panel at the bottom of
 *   the NiFi Connectors card (#kd-nifi-details-container) showing
 *   sync status and the extracted/copied NAR rows.
 *
 * Loaded after the main dashboard script. Safe to include only once.
 */
(function () {
  "use strict";

  if (window.__kdNifiAutoFixLoaded) {
    return;
  }
  window.__kdNifiAutoFixLoaded = true;

  var PICK_ENDPOINTS = ['/api/pick-nar', '/api/pick-zip', '/api/pick-folder'];
  var originalFetch = window.fetch ? window.fetch.bind(window) : null;

  if (!originalFetch) {
    return;
  }

  var activeDialog = null;
  var syncBusy = false;

  window.__kdLoadedConfig = window.__kdLoadedConfig || null;
  window.__kdLastZipCheck = window.__kdLastZipCheck || {};

  function makeJsonResponse(status, obj) {
    return new Response(JSON.stringify(obj), {
      status: status,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  }

  function urlFrom(input) {
    if (typeof input === 'string') {
      return input;
    }
    if (input && input.url) {
      return input.url;
    }
    return String(input);
  }

  function methodFrom(input, init) {
    if (init && init.method) {
      return String(init.method).toUpperCase();
    }
    if (input && input.method) {
      return String(input.method).toUpperCase();
    }
    return 'GET';
  }

  function joinPath() {
    var parts = Array.prototype.slice.call(arguments).filter(function (p) {
      return p !== null && p !== undefined && String(p).length > 0;
    });
    if (!parts.length) return '';
    // Detect platform from first absolute-looking segment
    var sample = String(parts[0]);
    var sep = (sample.indexOf('\\') >= 0 && sample.indexOf('/') < 0) ? '\\' : '/';
    // Also prefer / when running in a browser on Linux-served pages
    if (typeof navigator !== 'undefined' && /linux|x11|android/i.test(navigator.userAgent || '')) {
      sep = '/';
    }
    var out = String(parts[0]).replace(/[\\/]+$/, '');
    for (var i = 1; i < parts.length; i++) {
      out += sep + String(parts[i]).replace(/^[\\/]+/, '').replace(/[\\/]+$/, '');
    }
    return out;
  }

  function findConfigValue(obj, names, depth) {
    if (!obj || depth > 6) {
      return '';
    }

    var wanted = {};
    names.forEach(function (name) {
      wanted[name.toLowerCase()] = true;
    });

    if (Array.isArray(obj)) {
      for (var i = 0; i < obj.length; i += 1) {
        var found = findConfigValue(obj[i], names, depth + 1);
        if (found) {
          return found;
        }
      }
      return '';
    }

    if (typeof obj === 'object') {
      var keys = Object.keys(obj);
      for (var k = 0; k < keys.length; k += 1) {
        var key = keys[k];
        var value = obj[key];

        if (wanted[String(key).toLowerCase()]) {
          var text = value === null || value === undefined ? '' : String(value).trim();
          if (text) {
            return text;
          }
        }
      }

      for (var j = 0; j < keys.length; j += 1) {
        var nested = findConfigValue(obj[keys[j]], names, depth + 1);
        if (nested) {
          return nested;
        }
      }
    }

    return '';
  }

  function findInputValue(regex) {
    var inputs = Array.prototype.slice.call(
      document.querySelectorAll('input, select, textarea')
    );

    for (var i = 0; i < inputs.length; i += 1) {
      var el = inputs[i];
      var text = [
        el.id || '',
        el.name || '',
        el.placeholder || '',
        el.getAttribute('aria-label') || ''
      ].join(' ');

      if (regex.test(text) && el.value) {
        return el.value.trim();
      }
    }

    var labels = Array.prototype.slice.call(
      document.querySelectorAll('label, th, td, span, div')
    );

    for (var j = 0; j < labels.length; j += 1) {
      var label = labels[j];
      var label_text = label.textContent || '';

      if (label_text.length > 120) {
        continue;
      }

      if (regex.test(label_text)) {
        if (label.htmlFor) {
          var bound = document.getElementById(label.htmlFor);
          if (bound && bound.value) {
            return bound.value.trim();
          }
        }

        var child = label.querySelector('input, select, textarea');
        if (child && child.value) {
          return child.value.trim();
        }

        var nearby = label.parentElement
          ? label.parentElement.querySelector('input, select, textarea')
          : null;

        if (nearby && nearby.value) {
          return nearby.value.trim();
        }
      }
    }

    return '';
  }

  async function loadConfig() {
    try {
      var resp = await originalFetch('/api/load-config');
      var data = await resp.json();
      if (data && data.config) {
        window.__kdLoadedConfig = data.config;
      }
    } catch (err) {
      // Ignore config load errors.
    }
  }

  function getPaths() {
    var cfg = window.__kdLoadedConfig || {};
    var last = window.__kdLastZipCheck || {};

    var zipPath =
      last.zipPath ||
      findConfigValue(cfg, ['zipPath', 'zip_path', 'zipFolder', 'zip']) ||
      findInputValue(/zip.*path|zippath|zip folder/i) ||
      '';

    var setupPath =
      findConfigValue(cfg, ['setupPath', 'setup_path', 'setupRoot', 'setup']) ||
      findInputValue(/setup.*path|setuproot|setup folder/i) ||
      '';

    var basePath =
      findConfigValue(cfg, ['basePath', 'base_path', 'baseFolder', 'installPath', 'base']) ||
      findInputValue(/base.*path|install.*path|base folder/i) ||
      '';

    var targetPath = '';
    if (basePath) {
      targetPath = joinPath(basePath.replace(/[\\/]+$/, ''), 'NiFi', 'extensions');
    }

    return {
      zipPath: zipPath,
      setupPath: setupPath,
      basePath: basePath,
      targetPath: targetPath
    };
  }

  function hasNiFiIngest(data) {
    var results = data && Array.isArray(data.results) ? data.results : [];

    return results.some(function (item) {
      if (!item || item.status !== 'found') {
        return false;
      }

      var blob = [
        item.name || '',
        item.pattern || '',
        item.matched || '',
        Array.isArray(item.matches) ? item.matches.join(' ') : ''
      ].join(' ');

      return /nifi.*ingest|ingest.*nifi/i.test(blob);
    });
  }

  /**
   * Locate the "NiFi Connectors" card (#panel-nars in the current
   * dashboard markup) so the fix container can be anchored to the
   * bottom of that card specifically — not to whichever ancestor
   * happens to contain the heading text.
   *
   * Falls back to a text search for older/renamed markup, walking up
   * to the nearest `.panel` (or `section`/`fieldset`) ancestor so the
   * fallback still resolves to the whole card rather than an inner
   * strip like the panel header.
   */
  function findNifiCard() {
    var byId = document.getElementById('panel-nars');
    if (byId) {
      return byId;
    }

    var heading = Array.prototype.slice
      .call(document.querySelectorAll('h1, h2, h3, h4, h5, legend, span, div'))
      .find(function (el) {
        var text = (el.textContent || '').trim();
        return /NiFi Connectors/i.test(text) && text.length < 60;
      });

    if (!heading) {
      return null;
    }

    return (
      heading.closest('.panel') ||
      heading.closest('section, fieldset') ||
      heading.parentElement
    );
  }

  function ensureDetailsContainer() {
    if (!document.body) {
      return null;
    }

    var existing = document.getElementById('kd-nifi-details-container');
    if (existing) {
      return existing;
    }

    var card = findNifiCard();

    // <details>/<summary> gives us a collapsed-by-default disclosure
    // widget for free (no extra click handlers or state to manage) —
    // it only starts expanded if the "open" attribute is set, which
    // it deliberately is not.
    var container = document.createElement('details');
    container.id = 'kd-nifi-details-container';
    container.className = 'kd-nifi-details';

    var summary = document.createElement('summary');
    summary.setAttribute('title', 'Expand / collapse connector details');

    var toggle = document.createElement('span');
    toggle.className = 'kd-nifi-details-toggle';
    toggle.setAttribute('aria-hidden', 'true');
    toggle.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">' +
      '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>' +
      '</svg>';

    var title = document.createElement('span');
    title.className = 'kd-nifi-details-title';
    title.textContent = 'Connector details';

    var hint = document.createElement('span');
    hint.className = 'kd-nifi-details-hint';
    hint.textContent = 'collapsed';

    summary.appendChild(toggle);
    summary.appendChild(title);
    summary.appendChild(hint);

    var content = document.createElement('div');
    content.className = 'kd-nifi-details-content';

    var status = document.createElement('div');
    status.id = 'kd-nifi-details-status';
    status.className = 'kd-nifi-details-status';
    status.textContent = 'Ready.';

    var tableWrap = document.createElement('div');
    tableWrap.className = 'kd-nifi-details-table-wrap';

    var table = document.createElement('table');

    var thead = document.createElement('thead');
    var headRow = document.createElement('tr');

    ['Name', 'Source', 'Target', 'Status'].forEach(function (label) {
      var th = document.createElement('th');
      th.textContent = label;
      headRow.appendChild(th);
    });

    thead.appendChild(headRow);

    var tbody = document.createElement('tbody');
    tbody.id = 'kd-nifi-details-rows';

    table.appendChild(thead);
    table.appendChild(tbody);
    tableWrap.appendChild(table);

    var buttonRow = document.createElement('div');
    buttonRow.className = 'kd-nifi-details-actions';

    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn';
    button.textContent = 'Sync NiFi connectors now';
    button.addEventListener('click', function () {
      autoSyncNifi('manual');
    });

    buttonRow.appendChild(button);

    content.appendChild(status);
    content.appendChild(tableWrap);
    content.appendChild(buttonRow);

    container.appendChild(summary);
    container.appendChild(content);

    // Append as the last child of the card's .collapse-body (not the
    // card itself) so "Connector details" lives inside the same
    // show/hide scope as the rest of the NiFi Connectors card content —
    // visible only while that card is expanded, hidden along with
    // everything else when it's collapsed. Falls back to the card (or
    // document.body) if the expected .collapse-body wrapper isn't found,
    // so the container still renders somewhere on older/renamed markup.
    var body = card && card.querySelector('.collapse-body');
    (body || card || document.body).appendChild(container);

    return container;
  }

  function setStatus(message, isError) {
    var container = ensureDetailsContainer();
    if (!container) {
      return;
    }

    var status = document.getElementById('kd-nifi-details-status');
    if (!status) {
      return;
    }

    var stamp = new Date().toLocaleTimeString();
    status.textContent = '[' + stamp + '] ' + message;
    status.classList.remove('is-ok', 'is-error');
    status.classList.add(isError ? 'is-error' : 'is-ok');
  }

  function addCell(row, text) {
    var td = document.createElement('td');
    td.textContent = text || '';
    row.appendChild(td);
  }

  function renderResult(data) {
    if (!document.body) {
      return;
    }

    ensureDetailsContainer();

    var tbody = document.getElementById('kd-nifi-details-rows');
    if (!tbody) {
      return;
    }

    tbody.innerHTML = '';

    var sourceFiles = new Map();
    var targetFiles = new Map();

    (data.sourceFiles || []).forEach(function (item) {
      if (item && item.name) {
        sourceFiles.set(item.name, item);
      }
    });

    (data.targetFiles || []).forEach(function (item) {
      if (item && item.name) {
        targetFiles.set(item.name, item);
      }
    });

    var names = new Set();

    (data.extracted || []).forEach(function (name) {
      names.add(name);
    });

    (data.copied || []).forEach(function (name) {
      names.add(name);
    });

    sourceFiles.forEach(function (_value, name) {
      names.add(name);
    });

    targetFiles.forEach(function (_value, name) {
      names.add(name);
    });

    if (!names.size) {
      var emptyRow = document.createElement('tr');
      addCell(emptyRow, 'No .nar files found.');
      addCell(emptyRow, '');
      addCell(emptyRow, '');
      addCell(emptyRow, '');
      tbody.appendChild(emptyRow);
      return;
    }

    names.forEach(function (name) {
      var sourceItem = sourceFiles.get(name);
      var targetItem = targetFiles.get(name);

      var sourcePath = sourceItem && sourceItem.path
        ? sourceItem.path
        : (data.source ? joinPath(data.source, name) : '');

      var targetPath = targetItem && targetItem.path
        ? targetItem.path
        : (data.target ? joinPath(data.target, name) : '');

      var status = 'Unknown';

      if (targetItem || (data.copied || []).indexOf(name) !== -1) {
        status = 'Copied to target';
      } else if (sourceItem || (data.extracted || []).indexOf(name) !== -1) {
        status = 'Source only';
      }

      var row = document.createElement('tr');
      addCell(row, name);
      addCell(row, sourcePath);
      addCell(row, targetPath);
      addCell(row, status);
      tbody.appendChild(row);
    });
  }

  async function callExtractWithFallback(paths) {
    var autoBody = {
      zipPath: paths.zipPath,
      setupPath: paths.setupPath,
      basePath: paths.basePath,
      targetPath: paths.targetPath,
      pattern: '*NiFiIngest*.zip',
      reason: 'auto-sync'
    };

    var resp = await originalFetch('/api/auto-sync-nifi', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(autoBody)
    });

    var json = null;
    try {
      json = await resp.clone().json();
    } catch (err) {
      json = null;
    }

    var contentType = resp.headers.get('content-type') || '';

    // If the updated server endpoint does not exist, fall back to the
    // original extract endpoint.
    if (resp.status === 404 && contentType.indexOf('application/json') === -1) {
      var fallbackBody = {
        zipPath: paths.zipPath,
        setupPath: paths.setupPath,
        targetPath: paths.targetPath,
        pattern: '*NiFiIngest*.zip'
      };

      resp = await originalFetch('/api/extract-nars-from-zip', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(fallbackBody)
      });

      try {
        json = await resp.clone().json();
      } catch (err) {
        json = null;
      }
    }

    return {
      resp: resp,
      json: json
    };
  }

  async function autoSyncNifi(reason) {
    if (syncBusy) {
      return;
    }

    syncBusy = true;
    setStatus('Syncing NiFi connectors (' + reason + ')...');

    try {
      await loadConfig();

      var paths = getPaths();

      if (!paths.zipPath && !paths.setupPath && !paths.basePath) {
        setStatus(
          'Skipped: ZipPath / SetupPath / BasePath not found in UI or saved config.',
          true
        );
        return;
      }

      var result = await callExtractWithFallback(paths);

      if (result.json) {
        renderResult(result.json);

        if (result.json.ok) {
          var extracted = result.json.extractCount ||
            (result.json.extracted ? result.json.extracted.length : 0);

          var copied = result.json.copyCount ||
            (result.json.copied ? result.json.copied.length : 0);

          setStatus(
            'NiFi sync complete: extracted ' + extracted +
            ', copied ' + copied + '.'
          );
        } else {
          var message = result.json.error ||
            (result.json.copyErrors && result.json.copyErrors.length
              ? result.json.copyErrors.join('; ')
              : '') ||
            (result.json.extractionErrors && result.json.extractionErrors.length
              ? result.json.extractionErrors.join('; ')
              : '') ||
            'Unknown NiFi sync error';

          setStatus('NiFi sync finished with problems: ' + message, true);
        }
      } else {
        setStatus('NiFi sync failed: HTTP ' + result.resp.status, true);
      }
    } catch (err) {
      setStatus('NiFi sync error: ' + (err && err.message ? err.message : err), true);
    } finally {
      syncBusy = false;
    }
  }

  window.fetch = async function (input, init) {
    var url = urlFrom(input);
    var method = methodFrom(input, init);

    // Prevent duplicate native dialogs caused by duplicate UI requests.
    var isPickEndpoint = PICK_ENDPOINTS.some(function (endpoint) {
      return url.indexOf(endpoint) !== -1;
    });

    if (isPickEndpoint) {
      if (activeDialog) {
        return makeJsonResponse(423, {
          ok: false,
          cancelled: false,
          error: 'Another native dialog is already open'
        });
      }

      var dialogPromise = originalFetch(input, init);
      activeDialog = dialogPromise;

      dialogPromise.finally(function () {
        activeDialog = null;
      });

      return dialogPromise;
    }

    var response = await originalFetch(input, init);

    // Capture saved config when the dashboard loads it.
    if (url.indexOf('/api/load-config') !== -1) {
      try {
        response
          .clone()
          .json()
          .then(function (data) {
            if (data && data.config) {
              window.__kdLoadedConfig = data.config;
            }
          })
          .catch(function () {});
      } catch (err) {
        // Ignore.
      }
    }

    // When ZIP existence check completes, auto-extract NiFi NARs if
    // a NiFiIngest ZIP was found.
    if (url.indexOf('/api/check-zips') !== -1 && method === 'POST') {
      try {
        if (init && init.body && typeof init.body === 'string') {
          try {
            var requestBody = JSON.parse(init.body);
            if (requestBody && typeof requestBody === 'object') {
              window.__kdLastZipCheck = Object.assign(
                {},
                window.__kdLastZipCheck,
                requestBody
              );
            }
          } catch (err) {
            // Ignore malformed body.
          }
        }

        response
          .clone()
          .json()
          .then(function (data) {
            if (data && hasNiFiIngest(data)) {
              autoSyncNifi('check-zips');
            }
          })
          .catch(function () {});
      } catch (err) {
        // Ignore.
      }
    }

    return response;
  };

  function startup() {
    ensureDetailsContainer();

    setTimeout(async function () {
      await loadConfig();

      var paths = getPaths();
      if (paths.zipPath || paths.setupPath || paths.basePath) {
        autoSyncNifi('startup');
      }
    }, 1200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startup);
  } else {
    startup();
  }
})();
