// The owner's figures guards of portal/data_folder.h (Swap Force Online's main safety audit,
// settings-figures): PathInside, OwnerFigureWriteRefused and CopySharesOwnersFolder, on folders
// made under the temp folder (never the real Documents).

#include <filesystem>
#include <system_error>
#include <vector>

#include "portal/data_folder.h"
#include "test_util.h"

namespace fs = std::filesystem;
using skylanders::CopySharesOwnersFolder;
using skylanders::OwnerFigureWriteRefused;
using skylanders::PathInside;

int main() {
  const fs::path t = fs::temp_directory_path() / L"sp_data_folder_guard";
  std::error_code ec;
  fs::remove_all(t, ec);
  fs::create_directories(t / L"Docs" / L"Skylanders" / L"Figures" / L"Swap Force", ec);
  fs::create_directories(t / L"Picked" / L"Figures", ec);
  fs::create_directories(t / L"run" / L"copy" / L"figures", ec);
  REQUIRE_OR_RETURN(!ec);
  const fs::path owner_data = t / L"Docs" / L"Skylanders";
  const fs::path owner_figures = owner_data / L"Figures";
  const fs::path picked_figures = t / L"Picked" / L"Figures";
  const fs::path run_figures = t / L"run" / L"copy" / L"figures";

  // PathInside: the folder itself and anything under it, existing or not; never a sibling that only
  // shares the name's start; `..` resolved; case ignored (Windows); a trailing separator is the same
  // folder; empty is never inside.
  CHECK(PathInside(owner_figures, owner_figures));
  CHECK(PathInside(owner_figures / L"Swap Force" / L"Blast Zone.dump", owner_figures));
  CHECK(PathInside(owner_figures / L"New" / L"Not there yet.dump", owner_figures));
  CHECK(!PathInside(owner_data / L"Figures2" / L"x.dump", owner_figures));
  CHECK(!PathInside(owner_data, owner_figures));
  CHECK(PathInside(owner_figures / L".." / L"Figures" / L"x.dump", owner_figures));
  CHECK(!PathInside(owner_figures / L".." / L"Pictures" / L"x.png", owner_figures));
#ifdef _WIN32
  CHECK(PathInside(t / L"docs" / L"SKYLANDERS" / L"figures" / L"x.dump", owner_figures));
  CHECK(PathInside(owner_figures / L"x.dump", fs::path(owner_figures.native() + L"\\")));
#endif
  CHECK(!PathInside(fs::path(), owner_figures));
  CHECK(!PathInside(owner_figures, fs::path()));

  // OwnerFigureWriteRefused: a test copy's save into either owner figures folder (the default one
  // and the one picked in Core of Light) is refused; its own figures folder and anything else are
  // not; an empty owner entry is skipped.
  const std::vector<fs::path> owners = {picked_figures, owner_figures, fs::path()};
  CHECK(OwnerFigureWriteRefused(owner_figures / L"Swap Force" / L"Blast Zone.dump", owners, run_figures));
  CHECK(OwnerFigureWriteRefused(picked_figures / L"Trap Team" / L"Chompy Mage.dump", owners, run_figures));
#ifdef _WIN32
  CHECK(OwnerFigureWriteRefused(owner_figures / L"swap force" / L"..\\Blast Zone.dump", owners, run_figures));
#endif
  CHECK(!OwnerFigureWriteRefused(run_figures / L"Swap Force" / L"Blast Zone.dump", owners, run_figures));
  CHECK(!OwnerFigureWriteRefused(t / L"elsewhere" / L"Blast Zone.dump", owners, run_figures));
  CHECK(!OwnerFigureWriteRefused(owner_figures / L"x.dump", {}, run_figures));
  // An explicit --user_data_root of the owner's real game folder: its own figures are the owner's,
  // and it keeps saving them; the other owner folder (a picked data folder's) stays refused.
  CHECK(!OwnerFigureWriteRefused(owner_figures / L"Swap Force" / L"Blast Zone.dump", owners, owner_figures));
#ifdef _WIN32
  CHECK(!OwnerFigureWriteRefused(owner_figures / L"x.dump", owners, t / L"docs" / L"skylanders" / L"FIGURES\\"));
#endif
  CHECK(OwnerFigureWriteRefused(picked_figures / L"x.dump", owners, owner_figures));
  // A run folder inside an owner's figures folder is not the owner's folder: still refused.
  CHECK(OwnerFigureWriteRefused(owner_figures / L"Swap Force" / L"x.dump", owners, owner_figures / L"Swap Force"));

  // CopySharesOwnersFolder: a copy beside the owner's data folder whose name is not a game's
  // (Documents\sf-test, SharedBase -> Documents\Skylanders) shares the owner's folder; a game-named
  // folder (the owner's own game folder, or a copy named like a game anywhere) does not, nor a copy
  // elsewhere, a copy inside the data folder, or a root.
  const std::vector<fs::path> data_folders = {t / L"Picked", owner_data, fs::path()};
  CHECK(CopySharesOwnersFolder(t / L"Docs" / L"sf-test", data_folders));
#ifdef _WIN32
  CHECK(CopySharesOwnersFolder(t / L"docs" / L"SF-TEST", data_folders));
#endif
  CHECK(CopySharesOwnersFolder(t / L"Docs" / L"not there yet", data_folders));
  CHECK(!CopySharesOwnersFolder(owner_data / L"Swap Force", data_folders));
  CHECK(!CopySharesOwnersFolder(t / L"Docs" / L"Swap Force", data_folders));
  CHECK(!CopySharesOwnersFolder(t / L"run" / L"copy", data_folders));
  CHECK(!CopySharesOwnersFolder(owner_data / L"sf-test", data_folders));
  CHECK(!CopySharesOwnersFolder(t.root_path(), data_folders));
  CHECK(!CopySharesOwnersFolder(t / L"Docs" / L"sf-test", {}));
  CHECK(!CopySharesOwnersFolder(fs::path(), data_folders));
  // A picked data folder named Skylanders: a copy next to it shares it as well.
  fs::create_directories(t / L"Drive" / L"Skylanders", ec);
  CHECK(CopySharesOwnersFolder(t / L"Drive" / L"sf-test", {t / L"Drive" / L"Skylanders"}));

  fs::remove_all(t, ec);
  return Finish("data_folder");
}
