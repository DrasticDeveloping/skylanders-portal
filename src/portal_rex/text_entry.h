#pragma once

// A small text box over the game (text_entry.cpp), in the look of "Report a problem"
// (problem_note.h): a title, one line saying what to type, and the box. The keyboard types into it
// (Ctrl+V pastes, Enter accepts, Esc cancels); a controller pastes the clipboard with Y, clears the
// box with X, accepts with A and cancels with B. The game sees no input while it is open. Stage 8:
// F4 > Online > Join, where the player types or pastes the join code.

#include <cstddef>
#include <functional>
#include <string>

namespace rex::ui {
class ImGuiDrawer;
class WindowedAppContext;
}  // namespace rex::ui

namespace skylanders {

struct TextEntryRequest {
  std::string title_en, title_de;    // upper case, as the menus' titles ("JOIN A SESSION")
  std::string prompt_en, prompt_de;  // the line above the box
  std::string hint_en, hint_de;      // greyed in the empty box
  std::string accept_en, accept_de;  // A's hint ("Join")
  size_t max_length = 64;            // bytes the box takes (at most 255)
  bool upper_case = false;           // a-z typed become A-Z
  // The text when the player accepts (A or Enter with something in the box): on the UI thread,
  // after the box has closed. It must not block (start a thread for anything slow).
  std::function<void(const std::string& text)> on_accept;
};

// Call from ReXApp::OnCreateDialogs (RegisterSettingsMenu does).
void RegisterTextEntry(rex::ui::ImGuiDrawer* drawer, rex::ui::WindowedAppContext* app_context);

// Opens the box (UI thread), e.g. from a settings menu action row. false (nothing opened) while
// another menu or box is open, or before RegisterTextEntry.
bool OpenTextEntry(TextEntryRequest request);

void ShutdownTextEntry();

}  // namespace skylanders
