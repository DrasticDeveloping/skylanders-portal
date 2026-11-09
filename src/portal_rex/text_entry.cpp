#include "portal_rex/text_entry.h"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>

#include <imgui.h>
#include <rex/input/input.h>
#include <rex/logging.h>
#include <rex/ui/imgui_dialog.h>
#include <rex/ui/imgui_drawer.h>
#include <rex/ui/windowed_app_context.h>

#include "portal_rex/menu_draw.h"
#include "portal_rex/portal_menu_input.h"

namespace skylanders {
namespace {

using namespace menu_draw;

const std::string& Tr(const std::string& en, const std::string& de) { return GermanText() ? de : en; }
const char* Tr(const char* en, const char* de) { return GermanText() ? de : en; }

// New presses of A, B, X and Y on one controller. The first read only notes what is held already
// (the A that chose the settings menu's row is still down), as problem_note.cpp's pad does.
class Pad {
 public:
  struct Edges {
    bool accept = false, back = false, clear = false, paste = false;
  };
  Edges Read(const rex::input::X_INPUT_GAMEPAD& pad) {
    Edges e;
    const uint16_t buttons = pad.buttons;
    if (!seen_) {
      seen_ = true;
      prev_ = buttons;
      return e;
    }
    auto pressed = [&](uint16_t bit) { return (buttons & bit) && !(prev_ & bit); };
    e.accept = pressed(rex::input::X_INPUT_GAMEPAD_A);
    e.back = pressed(rex::input::X_INPUT_GAMEPAD_B);
    e.clear = pressed(rex::input::X_INPUT_GAMEPAD_X);
    e.paste = pressed(rex::input::X_INPUT_GAMEPAD_Y);
    prev_ = buttons;
    return e;
  }

 private:
  bool seen_ = false;
  uint16_t prev_ = 0;
};

class TextEntryDialog final : public rex::ui::ImGuiDialog {
 public:
  TextEntryDialog(rex::ui::ImGuiDrawer* drawer, TextEntryRequest request, std::function<void()> on_done)
      : ImGuiDialog(drawer), request_(std::move(request)), on_done_(std::move(on_done)) {
    request_.max_length = std::clamp<size_t>(request_.max_length, 1, sizeof(text_) - 1);
    menu_input::SetMenuOpen(true);
    menu_input::SetMenuCloser([this] { Close(); });
    REXLOG_INFO("Text entry: opened ({})", request_.title_en);
  }
  ~TextEntryDialog() override {
    if (!done_) menu_input::SetMenuOpen(false);
  }

  // Closes without accepting (B, Esc, or another menu taking over).
  void Close() {
    if (done_) return;
    done_ = true;
    menu_input::SetMenuOpen(false);
    if (on_done_) on_done_();
  }

 protected:
  void OnDraw(ImGuiIO& io) override {
    if (done_) return;
    SetScale(io.DisplaySize.y / 1080.0f);
    HandlePads();
    if (done_) return;
    ImGui::SetNextWindowPos({0, 0});
    ImGui::SetNextWindowSize(io.DisplaySize);
    ImGui::Begin("##text_entry", nullptr,
                 ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoBackground |
                     ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoNav);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float W = io.DisplaySize.x, H = io.DisplaySize.y;
    dl->AddRectFilled({0, 0}, {W, H}, Rgba(6, 10, 30, 140));
    const float pw = std::min(W * 0.8f, S(900));
    const float ph = S(300);
    const ImVec2 p0{(W - pw) / 2, (H - ph) / 2}, p1{(W + pw) / 2, (H + ph) / 2};
    DrawPanel(dl, p0, p1);
    OutlinedLabel(dl, TitleFont(), S(40), {p0.x + S(36), p0.y + S(22)}, kGold,
                  Tr(request_.title_en, request_.title_de), S(2.0f));
    Label(dl, BoldFont(), S(20), {p0.x + S(38), p0.y + S(80)}, kDim, Tr(request_.prompt_en, request_.prompt_de),
          pw - S(76));
    // The box. After a controller's paste or clear it gets a new id, so ImGui reads the text anew
    // (an active box keeps its own copy) and takes the keyboard again.
    ImGui::SetCursorScreenPos({p0.x + S(38), p0.y + S(140)});
    ImGui::PushItemWidth(pw - S(76));
    ImGui::PushID(generation_);
    if (focus_) {
      ImGui::SetKeyboardFocusHere();
      focus_ = false;
    }
    ImGuiInputTextFlags flags = ImGuiInputTextFlags_EnterReturnsTrue;
    if (request_.upper_case) flags |= ImGuiInputTextFlags_CharsUppercase;
    const bool enter = ImGui::InputTextWithHint("##text", Tr(request_.hint_en, request_.hint_de).c_str(), text_,
                                                request_.max_length + 1, flags);
    ImGui::PopID();
    ImGui::PopItemWidth();
    float x = p0.x + S(38);
    const float hy = p1.y - S(48);
    const std::string accept = Tr(request_.accept_en, request_.accept_de);
    x += Hint(dl, {x, hy}, "A", kAcceptGreen, accept.empty() ? Tr("OK", "OK") : accept.c_str(), true, "Enter");
    x += Hint(dl, {x, hy}, "Y", kYellow, Tr("Paste", "Einf\xC3\xBCgen"), true, "Ctrl+V");
    x += Hint(dl, {x, hy}, "X", kDetailsBlue, Tr("Clear", "Leeren"), true);
    Hint(dl, {x, hy}, "B", kBackRed, Tr("Cancel", "Abbrechen"), true, "Esc");
    ImGui::End();
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
      Close();
      return;
    }
    if (enter) accept_ = true;
    if (accept_) Accept();
  }

 private:
  void HandlePads() {
    Pad::Edges e;
    for (const menu_input::ConnectedDevice& device : menu_input::ConnectedDevices()) {
      if (!device.pollable) continue;
      const auto pad = menu_input::ReadPad(device.id);
      if (!pad) continue;
      const Pad::Edges r = pads_[uint64_t(device.id)].Read(*pad);
      e.accept |= r.accept;
      e.back |= r.back;
      e.clear |= r.clear;
      e.paste |= r.paste;
    }
    if (e.back) {
      Close();
      return;
    }
    if (e.clear) SetText("");
    if (e.paste) {
      const char* clip = ImGui::GetClipboardText();
      SetText(clip ? clip : "");
    }
    if (e.accept) accept_ = true;
  }

  // The box's text from a controller's paste or clear: line ends and tabs dropped (the box has one
  // line), cut at max_length, upper case where the box asks for it.
  void SetText(const char* text) {
    std::string t;
    for (const char* c = text; *c && t.size() < request_.max_length; ++c) {
      if (*c == '\r' || *c == '\n' || *c == '\t') continue;
      t.push_back(request_.upper_case && *c >= 'a' && *c <= 'z' ? char(*c - 'a' + 'A') : *c);
    }
    std::memcpy(text_, t.c_str(), t.size() + 1);
    ++generation_;
    focus_ = true;
  }

  // A or Enter: closes and hands the text on, if there is any (an empty box stays open and takes
  // the keyboard again: Enter left it).
  void Accept() {
    accept_ = false;
    const std::string text = text_;
    if (text.find_first_not_of(' ') == std::string::npos) {
      focus_ = true;
      return;
    }
    auto on_accept = request_.on_accept;
    REXLOG_INFO("Text entry: accepted ({})", request_.title_en);
    Close();  // the dialog goes after this frame (on_done_ defers it); `this` stays valid here
    if (on_accept) on_accept(text);
  }

  TextEntryRequest request_;
  std::function<void()> on_done_;
  bool done_ = false;
  bool focus_ = true;
  bool accept_ = false;
  int generation_ = 0;
  char text_[256] = {};
  std::unordered_map<uint64_t, Pad> pads_;
};

rex::ui::ImGuiDrawer* g_drawer = nullptr;
rex::ui::WindowedAppContext* g_app_context = nullptr;
std::unique_ptr<TextEntryDialog> g_dialog;  // UI thread only

}  // namespace

void RegisterTextEntry(rex::ui::ImGuiDrawer* drawer, rex::ui::WindowedAppContext* app_context) {
  g_drawer = drawer;
  g_app_context = app_context;
}

bool OpenTextEntry(TextEntryRequest request) {
  if (!g_drawer || !g_app_context || g_dialog || menu_input::MenuOpen()) return false;
  g_dialog = std::make_unique<TextEntryDialog>(g_drawer, std::move(request), [] {
    g_app_context->CallInUIThreadDeferred([] { g_dialog.reset(); });
  });
  return true;
}

void ShutdownTextEntry() { g_dialog.reset(); }

}  // namespace skylanders
