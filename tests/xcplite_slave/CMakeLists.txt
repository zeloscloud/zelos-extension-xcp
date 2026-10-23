# XCP test slave for tests/test_e2e.py (XCP_TEST_SLAVE), linked against
# Vector XCPlite (MIT, https://github.com/vectorgrp/XCPlite), fetched at a
# pinned commit. Build: `just xcplite-slave`.
cmake_minimum_required(VERSION 3.14)
project(xcp_test_slave LANGUAGES C CXX)
set(CMAKE_C_STANDARD 11)

include(FetchContent)
FetchContent_Declare(
    xcplite
    GIT_REPOSITORY https://github.com/vectorgrp/XCPlite.git
    GIT_TAG da324776c9c2274d9a25cba890851ca1be4fc5ac
)
FetchContent_MakeAvailable(xcplite)

add_executable(xcp_test_slave main.c)
target_link_libraries(xcp_test_slave PRIVATE xcplite)
