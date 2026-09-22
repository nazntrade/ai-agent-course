"use strict";

// Safe, dependency-free renderer for a small Markdown subset of model output.
//
// Only createElement/textContent are used, so HTML coming from the model is
// always shown as text. Links are limited to http, https and mailto; anything
// else keeps its inline-marked label without creating an anchor. The single
// public entry point is window.renderMarkdownInto.
(function () {
  "use strict";

  var ALLOWED_PROTOCOLS = { "http:": true, "https:": true, "mailto:": true };
  var WORD_RE = /[0-9A-Za-z_]/;
  var WHITESPACE_RE = /\s/;
  // A list marker needs trailing whitespace, so a line like "*italic*" stays a
  // paragraph instead of turning into a one-item list.
  var UNORDERED_RE = /^\s*[-*+]\s+(.*)$/;
  var ORDERED_RE = /^\s*\d+[.)]\s+(.*)$/;

  function isWordChar(character) {
    return WORD_RE.test(character);
  }

  function safeHref(raw) {
    var url;
    try {
      url = new URL(raw);
    } catch (error) {
      return null;
    }
    return ALLOWED_PROTOCOLS[url.protocol] === true ? url.href : null;
  }

  // Find the closing "**" that is not part of a longer star run, so
  // "**bold *italic***" closes after the nested italic span instead of
  // swallowing its final marker.
  function findClosingDoubleStar(text, from) {
    var index = text.indexOf("**", from);
    while (index !== -1) {
      if (text.charAt(index + 2) !== "*") {
        return index;
      }
      index = text.indexOf("**", index + 1);
    }
    return -1;
  }

  // Find a closing "*" or "_" for an italic span. The marker must not follow
  // whitespace, must not be the first star of a "**" pair and, for "_", must
  // not sit inside a word ("snake_case" stays literal).
  function findClosingSingle(text, from, character) {
    var index = text.indexOf(character, from);
    while (index !== -1) {
      var before = text.charAt(index - 1);
      var after = text.charAt(index + 1);
      var usable =
        index > from &&
        before.length > 0 &&
        !WHITESPACE_RE.test(before) &&
        !(character === "*" && after === "*") &&
        !(character === "_" && isWordChar(after));
      if (usable) {
        return index;
      }
      index = text.indexOf(character, index + 1);
    }
    return -1;
  }

  function canOpenItalic(text, index, character) {
    var next = text.charAt(index + 1);
    if (next.length === 0 || WHITESPACE_RE.test(next)) {
      return false;
    }
    if (character === "_" && isWordChar(text.charAt(index - 1))) {
      return false;
    }
    return true;
  }

  function parseLink(text, start) {
    var labelEnd = text.indexOf("]", start + 1);
    if (labelEnd === -1 || text.charAt(labelEnd + 1) !== "(") {
      return null;
    }
    var urlEnd = text.indexOf(")", labelEnd + 2);
    if (urlEnd === -1) {
      return null;
    }
    var label = text.slice(start + 1, labelEnd);
    var href = safeHref(text.slice(labelEnd + 2, urlEnd).trim());
    var node;
    if (href) {
      node = document.createElement("a");
      node.setAttribute("href", href);
      node.setAttribute("rel", "noopener noreferrer");
      node.setAttribute("target", "_blank");
    } else {
      // An unsafe or malformed URL keeps the inline-marked label as text and
      // never becomes a clickable link.
      node = document.createDocumentFragment();
    }
    parseInline(label, node);
    return { node: node, next: urlEnd + 1 };
  }

  function parseInline(text, parent) {
    var buffer = "";
    var index = 0;

    function flush() {
      if (buffer) {
        parent.appendChild(document.createTextNode(buffer));
        buffer = "";
      }
    }

    while (index < text.length) {
      var character = text.charAt(index);
      if (character === "`") {
        var codeEnd = text.indexOf("`", index + 1);
        if (codeEnd > index + 1) {
          flush();
          var code = document.createElement("code");
          code.textContent = text.slice(index + 1, codeEnd);
          parent.appendChild(code);
          index = codeEnd + 1;
          continue;
        }
      } else if (character === "*" && text.charAt(index + 1) === "*") {
        var boldEnd = findClosingDoubleStar(text, index + 2);
        if (boldEnd > index + 2) {
          flush();
          var strong = document.createElement("strong");
          parseInline(text.slice(index + 2, boldEnd), strong);
          parent.appendChild(strong);
          index = boldEnd + 2;
          continue;
        }
      } else if (character === "[") {
        var link = parseLink(text, index);
        if (link) {
          flush();
          parent.appendChild(link.node);
          index = link.next;
          continue;
        }
      } else if (character === "*" || character === "_") {
        if (canOpenItalic(text, index, character)) {
          var italicEnd = findClosingSingle(text, index + 1, character);
          if (italicEnd !== -1) {
            flush();
            var em = document.createElement("em");
            parseInline(text.slice(index + 1, italicEnd), em);
            parent.appendChild(em);
            index = italicEnd + 1;
            continue;
          }
        }
      }
      buffer += character;
      index += 1;
    }
    flush();
  }

  function appendList(parent, lines, start, tag) {
    var pattern = tag === "ul" ? UNORDERED_RE : ORDERED_RE;
    var list = document.createElement(tag);
    var index = start;
    while (index < lines.length) {
      var match = pattern.exec(lines[index]);
      if (!match) {
        break;
      }
      var item = document.createElement("li");
      parseInline(match[1], item);
      list.appendChild(item);
      index += 1;
    }
    parent.appendChild(list);
    return index;
  }

  function appendParagraph(parent, lines, start) {
    var collected = [];
    var index = start;
    while (index < lines.length) {
      if (UNORDERED_RE.test(lines[index]) || ORDERED_RE.test(lines[index])) {
        break;
      }
      collected.push(lines[index]);
      index += 1;
    }
    // The newlines stay in a text node; .text already renders them via pre-wrap.
    parseInline(collected.join("\n"), parent);
    return index;
  }

  function buildFragment(text) {
    var fragment = document.createDocumentFragment();
    var lines = text.split("\n");
    var index = 0;
    while (index < lines.length) {
      if (UNORDERED_RE.test(lines[index])) {
        index = appendList(fragment, lines, index, "ul");
      } else if (ORDERED_RE.test(lines[index])) {
        index = appendList(fragment, lines, index, "ol");
      } else {
        index = appendParagraph(fragment, lines, index);
      }
    }
    return fragment;
  }

  function renderMarkdownInto(container, rawText) {
    if (!container || typeof container.replaceChildren !== "function") {
      return;
    }
    var text = rawText === null || rawText === undefined ? "" : String(rawText);
    var fragment;
    try {
      fragment = buildFragment(text);
    } catch (error) {
      fragment = document.createDocumentFragment();
      fragment.appendChild(document.createTextNode(text));
    }
    try {
      // A full replacement keeps repeated streaming renders idempotent.
      container.replaceChildren(fragment);
    } catch (error) {
      container.textContent = text;
    }
  }

  window.renderMarkdownInto = renderMarkdownInto;
})();
