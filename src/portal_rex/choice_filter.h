#pragma once

// The search field of a settings row's drop-down (settings_menu.h Setting::choices): which entries
// match what was typed and the selection kept across a new search. Pure.

#include <cstddef>
#include <string>
#include <string_view>
#include <vector>

namespace skylanders::choice_filter {

inline char Lower(char c) { return (c >= 'A' && c <= 'Z') ? char(c - 'A' + 'a') : c; }

// Whether `word` is in `text`, ignoring ASCII case.
inline bool Contains(std::string_view text, std::string_view word) {
  if (word.empty()) return true;
  if (word.size() > text.size()) return false;
  for (size_t i = 0; i + word.size() <= text.size(); ++i) {
    size_t j = 0;
    while (j < word.size() && Lower(text[i + j]) == Lower(word[j])) ++j;
    if (j == word.size()) return true;
  }
  return false;
}

// The indices of the entries that hold every word of `query` (split at spaces), in the list's order;
// every entry for a query with no word.
inline std::vector<size_t> Match(const std::vector<std::string>& items, std::string_view query) {
  std::vector<std::string_view> words;
  size_t start = 0;
  while (start < query.size()) {
    while (start < query.size() && query[start] == ' ') ++start;
    size_t end = start;
    while (end < query.size() && query[end] != ' ') ++end;
    if (end > start) words.push_back(query.substr(start, end - start));
    start = end;
  }
  std::vector<size_t> out;
  for (size_t i = 0; i < items.size(); ++i) {
    bool all = true;
    for (std::string_view w : words) {
      if (!Contains(items[i], w)) {
        all = false;
        break;
      }
    }
    if (all) out.push_back(i);
  }
  return out;
}

// The selection's place in `shown` after a new search: the entry `keep` (an index into the items) if it
// is still shown, else the first; 0 for an empty list.
inline size_t Keep(const std::vector<size_t>& shown, size_t keep) {
  for (size_t i = 0; i < shown.size(); ++i) {
    if (shown[i] == keep) return i;
  }
  return 0;
}

}  // namespace skylanders::choice_filter
