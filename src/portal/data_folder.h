#pragma once

// The one folder the Skylanders games keep their data in (the owner's call, 2026-10-24): by default
// Documents\Skylanders, or the folder picked in Core of Light, with
//
//   Figures\  Pictures\  Textures\     shared by every game
//   Spyro's Adventure\  Giants\  ...   each game's saves, achievements and shader cache
//
// Before, each game had a folder of its own in Documents (Documents\ssarecomp, ...). A game or the
// launcher copies that over once; the old folder stays, with a note in it, until the launcher has
// asked whether it may go. No ReXGlue here: the launcher uses it as well as the games.

#include <cstdint>
#include <filesystem>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

namespace skylanders {

// Documents (the shell's known folder, so a moved Documents folder counts).
std::filesystem::path DocumentsFolder();
// Documents\Skylanders.
std::filesystem::path DefaultDataFolder();
// Where a picked data folder is written down for every game: %APPDATA%\Skylanders\data-folder.txt,
// one line, UTF-8. Not there: the default.
std::filesystem::path DataFolderPointer();
// Portable data (Core of Light #47, a stick that goes from PC to PC): the folder of the running
// executable or one up to three folders above it (a game in <Core of Light>\Games\<game>) that
// holds kPortableMarker; empty if none. Its data folder is "Skylanders" in it, whatever the drive.
// Not portable.txt: that one marks Core of Light's ZIP (#46), whose data stays in Documents.
inline constexpr const char* kPortableMarker = "portable-data.txt";
std::filesystem::path PortableFolder();
// The data folder: the portable one, else the pointer's, else Documents\Skylanders.
std::filesystem::path DataFolder();
// Writes the pointer (or removes it for the default folder); false if it couldn't.
bool SetDataFolder(const std::filesystem::path& folder);

// A game's folder in the data folder, by its name ("Spyro's Adventure", "Giants", "Swap Force",
// "Trap Team", "SuperChargers", "Imaginators"), and whether a folder name is one of them.
bool IsGameFolderName(const std::filesystem::path& name);
// The game folder's name for a game's executable name ("ssarecomp" -> "Spyro's Adventure"); empty
// for one it doesn't know.
std::string_view GameFolderForExe(std::string_view exe_name);

// The folder with Figures, Pictures and Textures for a game's user data: the data folder for a game
// folder in it (Documents\Skylanders\Giants -> Documents\Skylanders, also a profile's
// ...\Profiles\<id>\Giants -> ...\Profiles\<id>); else, as before, "Skylanders" next to it
// (Documents\giantsrecomp -> Documents\Skylanders; a test copy keeps its own).
std::filesystem::path SharedBase(const std::filesystem::path& user_data_root);

// Whether `path` is `folder` or lies inside it, as Windows compares paths: both made absolute and
// resolved as far as they exist (weakly_canonical: `.` and `..`, and the junctions and links on the
// way), then compared without regard to case. False when either is empty.
bool PathInside(const std::filesystem::path& path, const std::filesystem::path& folder);

// The owner's figures guard for a run with --user_data_root (a test copy; Swap Force Online's
// main safety audit): whether saving the figure file `file` must be refused because it lies inside
// one of `owner_figure_folders` (DataFolder()/Figures, DefaultDataFolder()/Figures) that is not
// `run_figures_folder`, the run's own figures folder (so an explicit --user_data_root of the owner's
// real game folder, whose own figures are the owner's, keeps saving them). Empty entries are
// skipped.
bool OwnerFigureWriteRefused(const std::filesystem::path& file,
                             const std::vector<std::filesystem::path>& owner_figure_folders,
                             const std::filesystem::path& run_figures_folder);

// The reverse: whether `folder` holds (is an ancestor of, or is) one of `owner_figure_folders` that
// is not `run_figures_folder`, as Documents\Skylanders or a drive root holds the owner's Figures.
// Such a folder as a figures folder would let the portal menu, which scans it recursively, list,
// restore and write the owner's figures. Empty entries and an empty `folder` are never refused.
bool FolderHoldsOwnerFigures(const std::filesystem::path& folder,
                             const std::vector<std::filesystem::path>& owner_figure_folders,
                             const std::filesystem::path& run_figures_folder);

// The guard for a figures folder a test copy is configured with (portal_figures_dir): refused when
// it lies inside an owner's figures folder (OwnerFigureWriteRefused) or holds one
// (FolderHoldsOwnerFigures); the run's own figures folder is the exception on both sides.
bool OwnerFiguresDirRefused(const std::filesystem::path& folder,
                            const std::vector<std::filesystem::path>& owner_figure_folders,
                            const std::filesystem::path& run_figures_folder);

// The guard for a figure file a test copy would write (the game's saves, a backup restore, New
// figure, a dump) or play (portal_figure): refused when it lies inside an owner's figures folder
// (OwnerFigureWriteRefused). A file beside such a folder (Documents\Skylanders\x.dump,
// Documents\x.dump) is not refused: one file cannot reach into the folder next to it; folders the
// portal scans or writes into get OwnerFiguresDirRefused. The run's own figures folder is the
// exception.
bool OwnerFigureFileRefused(const std::filesystem::path& file,
                            const std::vector<std::filesystem::path>& owner_figure_folders,
                            const std::filesystem::path& run_figures_folder);

// SharedBase maps a --user_data_root copy whose folder name is not a game's to "Skylanders" next
// to it; true when that is one of `owner_data_folders` (DataFolder(), DefaultDataFolder()), as for
// a copy at Documents\sf-test: such a copy would get the owner's real Figures folder, so it keeps a
// figures folder of its own (portal_rex SharedFiguresDir). False for a game-named folder, a root
// and empty entries.
bool CopySharesOwnersFolder(const std::filesystem::path& user_data_root,
                            const std::vector<std::filesystem::path>& owner_data_folders);

// The note a copy leaves in the old folder (its first line is the new folder).
inline constexpr const char* kMovedNote = "MOVED_TO.txt";

struct CopyResult {
  bool ok = false;
  size_t files = 0;
  uintmax_t bytes = 0;
  std::string error;  // UTF-8, empty when ok
};
// Copies every file under `from` to `to`, which must not exist yet: first into `to` + ".partial",
// renamed to `to` once each file is there with its size, so `to` is either complete or absent.
// `on_file` (optional) hears each file before it is copied, for a progress display.
CopyResult CopyFolderVerified(const std::filesystem::path& from, const std::filesystem::path& to,
                              const std::function<void(const std::filesystem::path&)>& on_file = {});
// Whether each file under `old_dir` is under `new_dir` too (before the old one may be deleted).
bool AllFilesThere(const std::filesystem::path& old_dir, const std::filesystem::path& new_dir);
// Leaves the note in `old_dir` that it was copied to `new_dir`.
void WriteMovedNote(const std::filesystem::path& old_dir, const std::filesystem::path& new_dir);

}  // namespace skylanders
