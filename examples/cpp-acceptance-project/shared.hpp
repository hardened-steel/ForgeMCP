#pragma once

// The real clangd acceptance gate deliberately keeps this header closed while
// renaming the use in good_main.cpp.  The definition must nevertheless be
// found here and the WorkspaceEdit must atomically update both files.
inline int shared_value = 42;
