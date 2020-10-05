#!/usr/bin/env python3

import argparse
import common
import functools
import multiprocessing
import os
import os.path
import pathlib
import re
import subprocess
import stat
import sys
import traceback
import shutil
import paths

EXCLUDED_PREFIXES = (
    "./generated/",
    "./thirdparty/",
    "./build",
    "./.git/",
    "./bazel-",
    "./.cache",
    "./source/extensions/extensions_build_config.bzl",
    "./bazel/toolchains/configs/",
    "./tools/testdata/check_format/",
    "./tools/pyformat/",
    "./third_party/",
    "./test/extensions/filters/http/wasm/test_data",
    "./test/extensions/filters/network/wasm/test_data",
    "./test/extensions/stats_sinks/wasm/test_data",
    "./test/extensions/bootstrap/wasm/test_data",
    "./test/extensions/common/wasm/test_data",
    "./test/extensions/access_loggers/wasm/test_data",
    "./source/extensions/common/wasm/ext",
    "./examples/wasm",
)
SUFFIXES = ("BUILD", "WORKSPACE", ".bzl", ".cc", ".h", ".java", ".m", ".md", ".mm", ".proto",
            ".rst")
DOCS_SUFFIX = (".md", ".rst")
PROTO_SUFFIX = (".proto")

# Files in these paths can make reference to protobuf stuff directly
GOOGLE_PROTOBUF_ALLOWLIST = ("ci/prebuilt", "source/common/protobuf", "api/test",
                             "test/extensions/bootstrap/wasm/test_data")
REPOSITORIES_BZL = "bazel/repositories.bzl"

# Files matching these exact names can reference real-world time. These include the class
# definitions for real-world time, the construction of them in main(), and perf annotation.
# For now it includes the validation server but that really should be injected too.
REAL_TIME_ALLOWLIST = ("./source/common/common/utility.h",
                       "./source/extensions/common/aws/utility.cc",
                       "./source/common/event/real_time_system.cc",
                       "./source/common/event/real_time_system.h", "./source/exe/main_common.cc",
                       "./source/exe/main_common.h", "./source/server/config_validation/server.cc",
                       "./source/common/common/perf_annotation.h",
                       "./test/common/common/log_macros_test.cc",
                       "./test/test_common/simulated_time_system.cc",
                       "./test/test_common/simulated_time_system.h",
                       "./test/test_common/test_time.cc", "./test/test_common/test_time.h",
                       "./test/test_common/utility.cc", "./test/test_common/utility.h",
                       "./test/integration/integration.h")

# Tests in these paths may make use of the Registry::RegisterFactory constructor or the
# REGISTER_FACTORY macro. Other locations should use the InjectFactory helper class to
# perform temporary registrations.
REGISTER_FACTORY_TEST_ALLOWLIST = ("./test/common/config/registry_test.cc",
                                   "./test/integration/clusters/", "./test/integration/filters/")

# Files in these paths can use MessageLite::SerializeAsString
SERIALIZE_AS_STRING_ALLOWLIST = (
    "./source/common/config/version_converter.cc",
    "./source/common/protobuf/utility.cc",
    "./source/extensions/filters/http/grpc_json_transcoder/json_transcoder_filter.cc",
    "./test/common/protobuf/utility_test.cc",
    "./test/common/config/version_converter_test.cc",
    "./test/common/grpc/codec_test.cc",
    "./test/common/grpc/codec_fuzz_test.cc",
    "./test/extensions/filters/http/common/fuzz/uber_filter.h",
    "./test/extensions/bootstrap/wasm/test_data/speed_cpp.cc",
)

# Files in these paths can use Protobuf::util::JsonStringToMessage
JSON_STRING_TO_MESSAGE_ALLOWLIST = ("./source/common/protobuf/utility.cc",
                                    "./test/extensions/bootstrap/wasm/test_data/speed_cpp.cc")

# Histogram names which are allowed to be suffixed with the unit symbol, all of the pre-existing
# ones were grandfathered as part of PR #8484 for backwards compatibility.
HISTOGRAM_WITH_SI_SUFFIX_ALLOWLIST = ("downstream_cx_length_ms", "downstream_cx_length_ms",
                                      "initialization_time_ms", "loop_duration_us", "poll_delay_us",
                                      "request_time_ms", "upstream_cx_connect_ms",
                                      "upstream_cx_length_ms")

# Files in these paths can use std::regex
STD_REGEX_ALLOWLIST = (
    "./source/common/common/utility.cc", "./source/common/common/regex.h",
    "./source/common/common/regex.cc", "./source/common/stats/tag_extractor_impl.h",
    "./source/common/stats/tag_extractor_impl.cc",
    "./source/common/formatter/substitution_formatter.cc",
    "./source/extensions/filters/http/squash/squash_filter.h",
    "./source/extensions/filters/http/squash/squash_filter.cc", "./source/server/admin/utils.h",
    "./source/server/admin/utils.cc", "./source/server/admin/stats_handler.h",
    "./source/server/admin/stats_handler.cc", "./source/server/admin/prometheus_stats.h",
    "./source/server/admin/prometheus_stats.cc", "./tools/clang_tools/api_booster/main.cc",
    "./tools/clang_tools/api_booster/proto_cxx_utils.cc", "./source/common/version/version.cc")

# Only one C++ file should instantiate grpc_init
GRPC_INIT_ALLOWLIST = ("./source/common/grpc/google_grpc_context.cc")

# These files should not throw exceptions. Add HTTP/1 when exceptions removed.
EXCEPTION_DENYLIST = ("./source/common/http/http2/codec_impl.h",
                      "./source/common/http/http2/codec_impl.cc")

CLANG_FORMAT_PATH = os.getenv("CLANG_FORMAT", "clang-format-10")
BUILDIFIER_PATH = paths.getBuildifier()
BUILDOZER_PATH = paths.getBuildozer()
ENVOY_BUILD_FIXER_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                                      "envoy_build_fixer.py")
HEADER_ORDER_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "header_order.py")
SUBDIR_SET = set(common.includeDirOrder())
INCLUDE_ANGLE = "#include <"
INCLUDE_ANGLE_LEN = len(INCLUDE_ANGLE)
PROTO_PACKAGE_REGEX = re.compile(r"^package (\S+);\n*", re.MULTILINE)
X_ENVOY_USED_DIRECTLY_REGEX = re.compile(r'.*\"x-envoy-.*\".*')
DESIGNATED_INITIALIZER_REGEX = re.compile(r"\{\s*\.\w+\s*\=")
MANGLED_PROTOBUF_NAME_REGEX = re.compile(r"envoy::[a-z0-9_:]+::[A-Z][a-z]\w*_\w*_[A-Z]{2}")
HISTOGRAM_SI_SUFFIX_REGEX = re.compile(r"(?<=HISTOGRAM\()[a-zA-Z0-9_]+_(b|kb|mb|ns|us|ms|s)(?=,)")
TEST_NAME_STARTING_LOWER_CASE_REGEX = re.compile(r"TEST(_.\(.*,\s|\()[a-z].*\)\s\{")
EXTENSIONS_CODEOWNERS_REGEX = re.compile(r'.*(extensions[^@]*\s+)(@.*)')
COMMENT_REGEX = re.compile(r"//|\*")
DURATION_VALUE_REGEX = re.compile(r'\b[Dd]uration\(([0-9.]+)')
PROTO_VALIDATION_STRING = re.compile(r'\bmin_bytes\b')
VERSION_HISTORY_NEW_LINE_REGEX = re.compile("\* ([a-z \-_]+): ([a-z:`]+)")
VERSION_HISTORY_SECTION_NAME = re.compile("^[A-Z][A-Za-z ]*$")
RELOADABLE_FLAG_REGEX = re.compile(".*(.)(envoy.reloadable_features.[^ ]*)\s.*")
# Check for punctuation in a terminal ref clause, e.g.
# :ref:`panic mode. <arch_overview_load_balancing_panic_threshold>`
REF_WITH_PUNCTUATION_REGEX = re.compile(".*\. <[^<]*>`\s*")
DOT_MULTI_SPACE_REGEX = re.compile("\\. +")

# yapf: disable
PROTOBUF_TYPE_ERRORS = {
    # Well-known types should be referenced from the ProtobufWkt namespace.
    "Protobuf::Any":                    "ProtobufWkt::Any",
    "Protobuf::Empty":                  "ProtobufWkt::Empty",
    "Protobuf::ListValue":              "ProtobufWkt::ListValue",
    "Protobuf::NULL_VALUE":             "ProtobufWkt::NULL_VALUE",
    "Protobuf::StringValue":            "ProtobufWkt::StringValue",
    "Protobuf::Struct":                 "ProtobufWkt::Struct",
    "Protobuf::Value":                  "ProtobufWkt::Value",

    # Other common mis-namespacing of protobuf types.
    "ProtobufWkt::Map":                 "Protobuf::Map",
    "ProtobufWkt::MapPair":             "Protobuf::MapPair",
    "ProtobufUtil::MessageDifferencer": "Protobuf::util::MessageDifferencer"
}
LIBCXX_REPLACEMENTS = {
    "absl::make_unique<": "std::make_unique<",
}

UNOWNED_EXTENSIONS = {
  "extensions/filters/http/ratelimit",
  "extensions/filters/http/buffer",
  "extensions/filters/http/rbac",
  "extensions/filters/http/ip_tagging",
  "extensions/filters/http/tap",
  "extensions/filters/http/health_check",
  "extensions/filters/http/cors",
  "extensions/filters/http/ext_authz",
  "extensions/filters/http/dynamo",
  "extensions/filters/http/lua",
  "extensions/filters/http/common",
  "extensions/filters/common",
  "extensions/filters/common/ratelimit",
  "extensions/filters/common/rbac",
  "extensions/filters/common/lua",
  "extensions/filters/listener/original_dst",
  "extensions/filters/listener/proxy_protocol",
  "extensions/stat_sinks/statsd",
  "extensions/stat_sinks/common",
  "extensions/stat_sinks/common/statsd",
  "extensions/health_checkers/redis",
  "extensions/access_loggers/grpc",
  "extensions/access_loggers/file",
  "extensions/common/tap",
  "extensions/transport_sockets/raw_buffer",
  "extensions/transport_sockets/tap",
  "extensions/tracers/zipkin",
  "extensions/tracers/dynamic_ot",
  "extensions/tracers/opencensus",
  "extensions/tracers/lightstep",
  "extensions/tracers/common",
  "extensions/tracers/common/ot",
  "extensions/retry/host/previous_hosts",
  "extensions/filters/network/ratelimit",
  "extensions/filters/network/client_ssl_auth",
  "extensions/filters/network/rbac",
  "extensions/filters/network/tcp_proxy",
  "extensions/filters/network/echo",
  "extensions/filters/network/ext_authz",
  "extensions/filters/network/redis_proxy",
  "extensions/filters/network/kafka",
  "extensions/filters/network/kafka/broker",
  "extensions/filters/network/kafka/protocol",
  "extensions/filters/network/kafka/serialization",
  "extensions/filters/network/mongo_proxy",
  "extensions/filters/network/common",
  "extensions/filters/network/common/redis",
}
# yapf: enable


class FormatChecker:

  def __init__(self, args):
    self.operation_type = args.operation_type
    self.target_path = args.target_path
    self.api_prefix = args.api_prefix
    self.api_shadow_root = args.api_shadow_prefix
    self.envoy_build_rule_check = not args.skip_envoy_build_rule_check
    self.namespace_check = args.namespace_check
    self.namespace_check_excluded_paths = args.namespace_check_excluded_paths + [
        "./tools/api_boost/testdata/",
        "./tools/clang_tools/",
    ]
    self.build_fixer_check_excluded_paths = args.build_fixer_check_excluded_paths + [
        "./bazel/external/",
        "./bazel/toolchains/",
        "./bazel/BUILD",
        "./tools/clang_tools",
    ]
    self.include_dir_order = args.include_dir_order

  # Map a line transformation function across each line of a file,
  # writing the result lines as requested.
  # If there is a clang format nesting or mismatch error, return the first occurrence
  def evaluateLines(self, path, line_xform, write=True):
    error_message = None
    format_flag = True
    output_lines = []
    for line_number, line in enumerate(self.readLines(path)):
      if line.find("// clang-format off") != -1:
        if not format_flag and error_message is None:
          error_message = "%s:%d: %s" % (path, line_number + 1, "clang-format nested off")
        format_flag = False
      if line.find("// clang-format on") != -1:
        if format_flag and error_message is None:
          error_message = "%s:%d: %s" % (path, line_number + 1, "clang-format nested on")
        format_flag = True
      if format_flag:
        output_lines.append(line_xform(line, line_number))
      else:
        output_lines.append(line)
    # We used to use fileinput in the older Python 2.7 script, but this doesn't do
    # inplace mode and UTF-8 in Python 3, so doing it the manual way.
    if write:
      pathlib.Path(path).write_text('\n'.join(output_lines), encoding='utf-8')
    if not format_flag and error_message is None:
      error_message = "%s:%d: %s" % (path, line_number + 1, "clang-format remains off")
    return error_message

  # Obtain all the lines in a given file.
  def readLines(self, path):
    return self.readFile(path).split('\n')

  # Read a UTF-8 encoded file as a str.
  def readFile(self, path):
    return pathlib.Path(path).read_text(encoding='utf-8')

  # lookPath searches for the given executable in all directories in PATH
  # environment variable. If it cannot be found, empty string is returned.
  def lookPath(self, executable):
    return shutil.which(executable) or ''

  # pathExists checks whether the given path exists. This function assumes that
  # the path is absolute and evaluates environment variables.
  def pathExists(self, executable):
    return os.path.exists(os.path.expandvars(executable))

  # executableByOthers checks whether the given path has execute permission for
  # others.
  def executableByOthers(self, executable):
    st = os.stat(os.path.expandvars(executable))
    return bool(st.st_mode & stat.S_IXOTH)

  # Check whether all needed external tools (clang-format, buildifier, buildozer) are
  # available.
  def checkTools(self):
    error_messages = []

    clang_format_abs_path = self.lookPath(CLANG_FORMAT_PATH)
    if clang_format_abs_path:
      if not self.executableByOthers(clang_format_abs_path):
        error_messages.append("command {} exists, but cannot be executed by other "
                              "users".format(CLANG_FORMAT_PATH))
    else:
      error_messages.append(
          "Command {} not found. If you have clang-format in version 10.x.x "
          "installed, but the binary name is different or it's not available in "
          "PATH, please use CLANG_FORMAT environment variable to specify the path. "
          "Examples:\n"
          "    export CLANG_FORMAT=clang-format-10.0.0\n"
          "    export CLANG_FORMAT=/opt/bin/clang-format-10\n"
          "    export CLANG_FORMAT=/usr/local/opt/llvm@10/bin/clang-format".format(
              CLANG_FORMAT_PATH))

    def checkBazelTool(name, path, var):
      bazel_tool_abs_path = self.lookPath(path)
      if bazel_tool_abs_path:
        if not self.executableByOthers(bazel_tool_abs_path):
          error_messages.append("command {} exists, but cannot be executed by other "
                                "users".format(path))
      elif self.pathExists(path):
        if not self.executableByOthers(path):
          error_messages.append("command {} exists, but cannot be executed by other "
                                "users".format(path))
      else:

        error_messages.append("Command {} not found. If you have {} installed, but the binary "
                              "name is different or it's not available in $GOPATH/bin, please use "
                              "{} environment variable to specify the path. Example:\n"
                              "    export {}=`which {}`\n"
                              "If you don't have {} installed, you can install it by:\n"
                              "    go get -u github.com/bazelbuild/buildtools/{}".format(
                                  path, name, var, var, name, name, name))

    checkBazelTool('buildifier', BUILDIFIER_PATH, 'BUILDIFIER_BIN')
    checkBazelTool('buildozer', BUILDOZER_PATH, 'BUILDOZER_BIN')

    return error_messages

  def checkNamespace(self, file_path):
    for excluded_path in self.namespace_check_excluded_paths:
      if file_path.startswith(excluded_path):
        return []

    nolint = "NOLINT(namespace-%s)" % self.namespace_check.lower()
    text = self.readFile(file_path)
    if not re.search("^\s*namespace\s+%s\s*{" % self.namespace_check, text, re.MULTILINE) and \
      not nolint in text:
      return [
          "Unable to find %s namespace or %s for file: %s" %
          (self.namespace_check, nolint, file_path)
      ]
    return []

  def packageNameForProto(self, file_path):
    package_name = None
    error_message = []
    result = PROTO_PACKAGE_REGEX.search(self.readFile(file_path))
    if result is not None and len(result.groups()) == 1:
      package_name = result.group(1)
    if package_name is None:
      error_message = ["Unable to find package name for proto file: %s" % file_path]

    return [package_name, error_message]

  # To avoid breaking the Lyft import, we just check for path inclusion here.
  def allowlistedForProtobufDeps(self, file_path):
    return (file_path.endswith(PROTO_SUFFIX) or file_path.endswith(REPOSITORIES_BZL) or \
            any(path_segment in file_path for path_segment in GOOGLE_PROTOBUF_ALLOWLIST))

  # Real-world time sources should not be instantiated in the source, except for a few
  # specific cases. They should be passed down from where they are instantied to where
  # they need to be used, e.g. through the ServerInstance, Dispatcher, or ClusterManager.
  def allowlistedForRealTime(self, file_path):
    if file_path.endswith(".md"):
      return True
    return file_path in REAL_TIME_ALLOWLIST

  def allowlistedForRegisterFactory(self, file_path):
    if not file_path.startswith("./test/"):
      return True

    return any(file_path.startswith(prefix) for prefix in REGISTER_FACTORY_TEST_ALLOWLIST)

  def allowlistedForSerializeAsString(self, file_path):
    return file_path in SERIALIZE_AS_STRING_ALLOWLIST or file_path.endswith(DOCS_SUFFIX)

  def allowlistedForJsonStringToMessage(self, file_path):
    return file_path in JSON_STRING_TO_MESSAGE_ALLOWLIST

  def allowlistedForHistogramSiSuffix(self, name):
    return name in HISTOGRAM_WITH_SI_SUFFIX_ALLOWLIST

  def allowlistedForStdRegex(self, file_path):
    return file_path.startswith("./test") or file_path in STD_REGEX_ALLOWLIST or file_path.endswith(
        DOCS_SUFFIX)

  def allowlistedForGrpcInit(self, file_path):
    return file_path in GRPC_INIT_ALLOWLIST

  def allowlistedForUnpackTo(self, file_path):
    return file_path.startswith("./test") or file_path in [
        "./source/common/protobuf/utility.cc", "./source/common/protobuf/utility.h"
    ]

  def denylistedForExceptions(self, file_path):
    # Returns true when it is a non test header file or the file_path is in DENYLIST or
    # it is under toos/testdata subdirectory.
    if file_path.endswith(DOCS_SUFFIX):
      return False

    return (file_path.endswith('.h') and not file_path.startswith("./test/")) or file_path in EXCEPTION_DENYLIST \
        or self.isInSubdir(file_path, 'tools/testdata')

  def isApiFile(self, file_path):
    return file_path.startswith(self.api_prefix) or file_path.startswith(self.api_shadow_root)

  def isBuildFile(self, file_path):
    basename = os.path.basename(file_path)
    if basename in {"BUILD", "BUILD.bazel"} or basename.endswith(".BUILD"):
      return True
    return False

  def isExternalBuildFile(self, file_path):
    return self.isBuildFile(file_path) and (file_path.startswith("./bazel/external/") or
                                            file_path.startswith("./tools/clang_tools"))

  def isStarlarkFile(self, file_path):
    return file_path.endswith(".bzl")

  def isWorkspaceFile(self, file_path):
    return os.path.basename(file_path) == "WORKSPACE"

  def isBuildFixerExcludedFile(self, file_path):
    for excluded_path in self.build_fixer_check_excluded_paths:
      if file_path.startswith(excluded_path):
        return True
    return False

  def hasInvalidAngleBracketDirectory(self, line):
    if not line.startswith(INCLUDE_ANGLE):
      return False
    path = line[INCLUDE_ANGLE_LEN:]
    slash = path.find("/")
    if slash == -1:
      return False
    subdir = path[0:slash]
    return subdir in SUBDIR_SET

  def checkCurrentReleaseNotes(self, file_path, error_messages):
    first_word_of_prior_line = ''
    next_word_to_check = ''  # first word after :
    prior_line = ''

    def endsWithPeriod(prior_line):
      if not prior_line:
        return True  # Don't punctuation-check empty lines.
      if prior_line.endswith('.'):
        return True  # Actually ends with .
      if prior_line.endswith('`') and REF_WITH_PUNCTUATION_REGEX.match(prior_line):
        return True  # The text in the :ref ends with a .
      return False

    for line_number, line in enumerate(self.readLines(file_path)):

      def reportError(message):
        error_messages.append("%s:%d: %s" % (file_path, line_number + 1, message))

      if VERSION_HISTORY_SECTION_NAME.match(line):
        if line == "Deprecated":
          # The deprecations section is last, and does not have enforced formatting.
          break

        # Reset all parsing at the start of a section.
        first_word_of_prior_line = ''
        next_word_to_check = ''  # first word after :
        prior_line = ''

      # make sure flags are surrounded by ``s
      flag_match = RELOADABLE_FLAG_REGEX.match(line)
      if flag_match:
        if not flag_match.groups()[0].startswith('`'):
          reportError("Flag `%s` should be enclosed in back ticks" % flag_match.groups()[1])

      if line.startswith("* "):
        if not endsWithPeriod(prior_line):
          reportError("The following release note does not end with a '.'\n %s" % prior_line)

        match = VERSION_HISTORY_NEW_LINE_REGEX.match(line)
        if not match:
          reportError("Version history line malformed. "
                      "Does not match VERSION_HISTORY_NEW_LINE_REGEX in check_format.py\n %s" %
                      line)
        else:
          first_word = match.groups()[0]
          next_word = match.groups()[1]
          # Do basic alphabetization checks of the first word on the line and the
          # first word after the :
          if first_word_of_prior_line and first_word_of_prior_line > first_word:
            reportError(
                "Version history not in alphabetical order (%s vs %s): please check placement of line\n %s. "
                % (first_word_of_prior_line, first_word, line))
          if first_word_of_prior_line == first_word and next_word_to_check and next_word_to_check > next_word:
            reportError(
                "Version history not in alphabetical order (%s vs %s): please check placement of line\n %s. "
                % (next_word_to_check, next_word, line))
          first_word_of_prior_line = first_word
          next_word_to_check = next_word

          prior_line = line
      elif not line:
        # If we hit the end of this release note block block, check the prior line.
        if not endsWithPeriod(prior_line):
          reportError("The following release note does not end with a '.'\n %s" % prior_line)
      elif prior_line:
        prior_line += line

  def checkFileContents(self, file_path, checker):
    error_messages = []

    if file_path.endswith("version_history/current.rst"):
      # Version file checking has enough special cased logic to merit its own checks.
      # This only validates entries for the current release as very old release
      # notes have a different format.
      self.checkCurrentReleaseNotes(file_path, error_messages)

    def checkFormatErrors(line, line_number):

      def reportError(message):
        error_messages.append("%s:%d: %s" % (file_path, line_number + 1, message))

      checker(line, file_path, reportError)

    evaluate_failure = self.evaluateLines(file_path, checkFormatErrors, False)
    if evaluate_failure is not None:
      error_messages.append(evaluate_failure)

    return error_messages

  def fixSourceLine(self, line, line_number):
    # Strip double space after '.'  This may prove overenthusiastic and need to
    # be restricted to comments and metadata files but works for now.
    line = re.sub(DOT_MULTI_SPACE_REGEX, ". ", line)

    if self.hasInvalidAngleBracketDirectory(line):
      line = line.replace("<", '"').replace(">", '"')

    # Fix incorrect protobuf namespace references.
    for invalid_construct, valid_construct in PROTOBUF_TYPE_ERRORS.items():
      line = line.replace(invalid_construct, valid_construct)

    # Use recommended cpp stdlib
    for invalid_construct, valid_construct in LIBCXX_REPLACEMENTS.items():
      line = line.replace(invalid_construct, valid_construct)

    return line

  # We want to look for a call to condvar.waitFor, but there's no strong pattern
  # to the variable name of the condvar. If we just look for ".waitFor" we'll also
  # pick up time_system_.waitFor(...), and we don't want to return true for that
  # pattern. But in that case there is a strong pattern of using time_system in
  # various spellings as the variable name.
  def hasCondVarWaitFor(self, line):
    wait_for = line.find(".waitFor(")
    if wait_for == -1:
      return False
    preceding = line[0:wait_for]
    if preceding.endswith("time_system") or preceding.endswith("timeSystem()") or \
      preceding.endswith("time_system_"):
      return False
    return True

  # Determines whether the filename is either in the specified subdirectory, or
  # at the top level. We consider files in the top level for the benefit of
  # the check_format testcases in tools/testdata/check_format.
  def isInSubdir(self, filename, *subdirs):
    # Skip this check for check_format's unit-tests.
    if filename.count("/") <= 1:
      return True
    for subdir in subdirs:
      if filename.startswith('./' + subdir + '/'):
        return True
    return False

  # Determines if given token exists in line without leading or trailing token characters
  # e.g. will return True for a line containing foo() but not foo_bar() or baz_foo
  def tokenInLine(self, token, line):
    index = 0
    while True:
      index = line.find(token, index)
      # the following check has been changed from index < 1 to index < 0 because
      # this function incorrectly returns false when the token in question is the
      # first one in a line. The following line returns false when the token is present:
      # (no leading whitespace) violating_symbol foo;
      if index < 0:
        break
      if index == 0 or not (line[index - 1].isalnum() or line[index - 1] == '_'):
        if index + len(token) >= len(line) or not (line[index + len(token)].isalnum() or
                                                   line[index + len(token)] == '_'):
          return True
      index = index + 1
    return False

  def checkSourceLine(self, line, file_path, reportError):
    # Check fixable errors. These may have been fixed already.
    if line.find(".  ") != -1:
      reportError("over-enthusiastic spaces")
    if self.isInSubdir(file_path, 'source', 'include') and X_ENVOY_USED_DIRECTLY_REGEX.match(line):
      reportError(
          "Please do not use the raw literal x-envoy in source code.  See Envoy::Http::PrefixValue."
      )
    if self.hasInvalidAngleBracketDirectory(line):
      reportError("envoy includes should not have angle brackets")
    for invalid_construct, valid_construct in PROTOBUF_TYPE_ERRORS.items():
      if invalid_construct in line:
        reportError("incorrect protobuf type reference %s; "
                    "should be %s" % (invalid_construct, valid_construct))
    for invalid_construct, valid_construct in LIBCXX_REPLACEMENTS.items():
      if invalid_construct in line:
        reportError("term %s should be replaced with standard library term %s" %
                    (invalid_construct, valid_construct))
    # Do not include the virtual_includes headers.
    if re.search("#include.*/_virtual_includes/", line):
      reportError("Don't include the virtual includes headers.")

    # Some errors cannot be fixed automatically, and actionable, consistent,
    # navigable messages should be emitted to make it easy to find and fix
    # the errors by hand.
    if not self.allowlistedForProtobufDeps(file_path):
      if '"google/protobuf' in line or "google::protobuf" in line:
        reportError("unexpected direct dependency on google.protobuf, use "
                    "the definitions in common/protobuf/protobuf.h instead.")
    if line.startswith("#include <mutex>") or line.startswith("#include <condition_variable"):
      # We don't check here for std::mutex because that may legitimately show up in
      # comments, for example this one.
      reportError("Don't use <mutex> or <condition_variable*>, switch to "
                  "Thread::MutexBasicLockable in source/common/common/thread.h")
    if line.startswith("#include <shared_mutex>"):
      # We don't check here for std::shared_timed_mutex because that may
      # legitimately show up in comments, for example this one.
      reportError("Don't use <shared_mutex>, use absl::Mutex for reader/writer locks.")
    if not self.allowlistedForRealTime(file_path) and not "NO_CHECK_FORMAT(real_time)" in line:
      if "RealTimeSource" in line or \
        ("RealTimeSystem" in line and not "TestRealTimeSystem" in line) or \
        "std::chrono::system_clock::now" in line or "std::chrono::steady_clock::now" in line or \
        "std::this_thread::sleep_for" in line or self.hasCondVarWaitFor(line):
        reportError("Don't reference real-world time sources from production code; use injection")
    duration_arg = DURATION_VALUE_REGEX.search(line)
    if duration_arg and duration_arg.group(1) != "0" and duration_arg.group(1) != "0.0":
      # Matching duration(int-const or float-const) other than zero
      reportError(
          "Don't use ambiguous duration(value), use an explicit duration type, e.g. Event::TimeSystem::Milliseconds(value)"
      )
    if not self.allowlistedForRegisterFactory(file_path):
      if "Registry::RegisterFactory<" in line or "REGISTER_FACTORY" in line:
        reportError("Don't use Registry::RegisterFactory or REGISTER_FACTORY in tests, "
                    "use Registry::InjectFactory instead.")
    if not self.allowlistedForUnpackTo(file_path):
      if "UnpackTo" in line:
        reportError("Don't use UnpackTo() directly, use MessageUtil::unpackTo() instead")
    # Check that we use the absl::Time library
    if self.tokenInLine("std::get_time", line):
      if "test/" in file_path:
        reportError("Don't use std::get_time; use TestUtility::parseTime in tests")
      else:
        reportError("Don't use std::get_time; use the injectable time system")
    if self.tokenInLine("std::put_time", line):
      reportError("Don't use std::put_time; use absl::Time equivalent instead")
    if self.tokenInLine("gmtime", line):
      reportError("Don't use gmtime; use absl::Time equivalent instead")
    if self.tokenInLine("mktime", line):
      reportError("Don't use mktime; use absl::Time equivalent instead")
    if self.tokenInLine("localtime", line):
      reportError("Don't use localtime; use absl::Time equivalent instead")
    if self.tokenInLine("strftime", line):
      reportError("Don't use strftime; use absl::FormatTime instead")
    if self.tokenInLine("strptime", line):
      reportError("Don't use strptime; use absl::FormatTime instead")
    if self.tokenInLine("strerror", line):
      reportError("Don't use strerror; use Envoy::errorDetails instead")
    # Prefer using abseil hash maps/sets over std::unordered_map/set for performance optimizations and
    # non-deterministic iteration order that exposes faulty assertions.
    # See: https://abseil.io/docs/cpp/guides/container#hash-tables
    if "std::unordered_map" in line:
      reportError("Don't use std::unordered_map; use absl::flat_hash_map instead or "
                  "absl::node_hash_map if pointer stability of keys/values is required")
    if "std::unordered_set" in line:
      reportError("Don't use std::unordered_set; use absl::flat_hash_set instead or "
                  "absl::node_hash_set if pointer stability of keys/values is required")
    if "std::atomic_" in line:
      # The std::atomic_* free functions are functionally equivalent to calling
      # operations on std::atomic<T> objects, so prefer to use that instead.
      reportError("Don't use free std::atomic_* functions, use std::atomic<T> members instead.")
    # Block usage of certain std types/functions as iOS 11 and macOS 10.13
    # do not support these at runtime.
    # See: https://github.com/envoyproxy/envoy/issues/12341
    if self.tokenInLine("std::any", line):
      reportError("Don't use std::any; use absl::any instead")
    if self.tokenInLine("std::get_if", line):
      reportError("Don't use std::get_if; use absl::get_if instead")
    if self.tokenInLine("std::holds_alternative", line):
      reportError("Don't use std::holds_alternative; use absl::holds_alternative instead")
    if self.tokenInLine("std::make_optional", line):
      reportError("Don't use std::make_optional; use absl::make_optional instead")
    if self.tokenInLine("std::monostate", line):
      reportError("Don't use std::monostate; use absl::monostate instead")
    if self.tokenInLine("std::optional", line):
      reportError("Don't use std::optional; use absl::optional instead")
    if self.tokenInLine("std::string_view", line):
      reportError("Don't use std::string_view; use absl::string_view instead")
    if self.tokenInLine("std::variant", line):
      reportError("Don't use std::variant; use absl::variant instead")
    if self.tokenInLine("std::visit", line):
      reportError("Don't use std::visit; use absl::visit instead")
    if "__attribute__((packed))" in line and file_path != "./include/envoy/common/platform.h":
      # __attribute__((packed)) is not supported by MSVC, we have a PACKED_STRUCT macro that
      # can be used instead
      reportError("Don't use __attribute__((packed)), use the PACKED_STRUCT macro defined "
                  "in include/envoy/common/platform.h instead")
    if DESIGNATED_INITIALIZER_REGEX.search(line):
      # Designated initializers are not part of the C++14 standard and are not supported
      # by MSVC
      reportError("Don't use designated initializers in struct initialization, "
                  "they are not part of C++14")
    if " ?: " in line:
      # The ?: operator is non-standard, it is a GCC extension
      reportError("Don't use the '?:' operator, it is a non-standard GCC extension")
    if line.startswith("using testing::Test;"):
      reportError("Don't use 'using testing::Test;, elaborate the type instead")
    if line.startswith("using testing::TestWithParams;"):
      reportError("Don't use 'using testing::Test;, elaborate the type instead")
    if TEST_NAME_STARTING_LOWER_CASE_REGEX.search(line):
      # Matches variants of TEST(), TEST_P(), TEST_F() etc. where the test name begins
      # with a lowercase letter.
      reportError("Test names should be CamelCase, starting with a capital letter")
    if not self.allowlistedForSerializeAsString(file_path) and "SerializeAsString" in line:
      # The MessageLite::SerializeAsString doesn't generate deterministic serialization,
      # use MessageUtil::hash instead.
      reportError(
          "Don't use MessageLite::SerializeAsString for generating deterministic serialization, use MessageUtil::hash instead."
      )
    if not self.allowlistedForJsonStringToMessage(file_path) and "JsonStringToMessage" in line:
      # Centralize all usage of JSON parsing so it is easier to make changes in JSON parsing
      # behavior.
      reportError("Don't use Protobuf::util::JsonStringToMessage, use TestUtility::loadFromJson.")

    if self.isInSubdir(file_path, 'source') and file_path.endswith('.cc') and \
      ('.counterFromString(' in line or '.gaugeFromString(' in line or \
        '.histogramFromString(' in line or '.textReadoutFromString(' in line or \
        '->counterFromString(' in line or '->gaugeFromString(' in line or \
        '->histogramFromString(' in line or '->textReadoutFromString(' in line):
      reportError("Don't lookup stats by name at runtime; use StatName saved during construction")

    if MANGLED_PROTOBUF_NAME_REGEX.search(line):
      reportError("Don't use mangled Protobuf names for enum constants")

    hist_m = HISTOGRAM_SI_SUFFIX_REGEX.search(line)
    if hist_m and not self.allowlistedForHistogramSiSuffix(hist_m.group(0)):
      reportError(
          "Don't suffix histogram names with the unit symbol, "
          "it's already part of the histogram object and unit-supporting sinks can use this information natively, "
          "other sinks can add the suffix automatically on flush should they prefer to do so.")

    if not self.allowlistedForStdRegex(file_path) and "std::regex" in line:
      reportError("Don't use std::regex in code that handles untrusted input. Use RegexMatcher")

    if not self.allowlistedForGrpcInit(file_path):
      grpc_init_or_shutdown = line.find("grpc_init()")
      grpc_shutdown = line.find("grpc_shutdown()")
      if grpc_init_or_shutdown == -1 or (grpc_shutdown != -1 and
                                         grpc_shutdown < grpc_init_or_shutdown):
        grpc_init_or_shutdown = grpc_shutdown
      if grpc_init_or_shutdown != -1:
        comment = line.find("// ")
        if comment == -1 or comment > grpc_init_or_shutdown:
          reportError("Don't call grpc_init() or grpc_shutdown() directly, instantiate " +
                      "Grpc::GoogleGrpcContext. See #8282")

    if self.denylistedForExceptions(file_path):
      # Skpping cases where 'throw' is a substring of a symbol like in "foothrowBar".
      if "throw" in line.split():
        comment_match = COMMENT_REGEX.search(line)
        if comment_match is None or comment_match.start(0) > line.find("throw"):
          reportError("Don't introduce throws into exception-free files, use error " +
                      "statuses instead.")

    if "lua_pushlightuserdata" in line:
      reportError(
          "Don't use lua_pushlightuserdata, since it can cause unprotected error in call to" +
          "Lua API (bad light userdata pointer) on ARM64 architecture. See " +
          "https://github.com/LuaJIT/LuaJIT/issues/450#issuecomment-433659873 for details.")

    if file_path.endswith(PROTO_SUFFIX):
      exclude_path = ['v1', 'v2', 'generated_api_shadow']
      result = PROTO_VALIDATION_STRING.search(line)
      if result is not None:
        if not any(x in file_path for x in exclude_path):
          reportError("min_bytes is DEPRECATED, Use min_len.")

  def checkBuildLine(self, line, file_path, reportError):
    if "@bazel_tools" in line and not (self.isStarlarkFile(file_path) or
                                       file_path.startswith("./bazel/") or
                                       "python/runfiles" in line):
      reportError("unexpected @bazel_tools reference, please indirect via a definition in //bazel")
    if not self.allowlistedForProtobufDeps(file_path) and '"protobuf"' in line:
      reportError("unexpected direct external dependency on protobuf, use "
                  "//source/common/protobuf instead.")
    if (self.envoy_build_rule_check and not self.isStarlarkFile(file_path) and
        not self.isWorkspaceFile(file_path) and not self.isExternalBuildFile(file_path) and
        "@envoy//" in line):
      reportError("Superfluous '@envoy//' prefix")

  def fixBuildLine(self, file_path, line, line_number):
    if (self.envoy_build_rule_check and not self.isStarlarkFile(file_path) and
        not self.isWorkspaceFile(file_path) and not self.isExternalBuildFile(file_path)):
      line = line.replace("@envoy//", "//")
    return line

  def fixBuildPath(self, file_path):
    self.evaluateLines(file_path, functools.partial(self.fixBuildLine, file_path))

    error_messages = []

    # TODO(htuch): Add API specific BUILD fixer script.
    if not self.isBuildFixerExcludedFile(file_path) and not self.isApiFile(
        file_path) and not self.isStarlarkFile(file_path) and not self.isWorkspaceFile(file_path):
      if os.system("%s %s %s" % (ENVOY_BUILD_FIXER_PATH, file_path, file_path)) != 0:
        error_messages += ["envoy_build_fixer rewrite failed for file: %s" % file_path]

    if os.system("%s -lint=fix -mode=fix %s" % (BUILDIFIER_PATH, file_path)) != 0:
      error_messages += ["buildifier rewrite failed for file: %s" % file_path]
    return error_messages

  def checkBuildPath(self, file_path):
    error_messages = []

    if not self.isBuildFixerExcludedFile(file_path) and not self.isApiFile(
        file_path) and not self.isStarlarkFile(file_path) and not self.isWorkspaceFile(file_path):
      command = "%s %s | diff %s -" % (ENVOY_BUILD_FIXER_PATH, file_path, file_path)
      error_messages += self.executeCommand(command, "envoy_build_fixer check failed", file_path)

    if self.isBuildFile(file_path) and (file_path.startswith(self.api_prefix + "envoy") or
                                        file_path.startswith(self.api_shadow_root + "envoy")):
      found = False
      for line in self.readLines(file_path):
        if "api_proto_package(" in line:
          found = True
          break
      if not found:
        error_messages += ["API build file does not provide api_proto_package()"]

    command = "%s -mode=diff %s" % (BUILDIFIER_PATH, file_path)
    error_messages += self.executeCommand(command, "buildifier check failed", file_path)
    error_messages += self.checkFileContents(file_path, self.checkBuildLine)
    return error_messages

  def fixSourcePath(self, file_path):
    self.evaluateLines(file_path, self.fixSourceLine)

    error_messages = []

    if not file_path.endswith(DOCS_SUFFIX):
      if not file_path.endswith(PROTO_SUFFIX):
        error_messages += self.fixHeaderOrder(file_path)
      error_messages += self.clangFormat(file_path)
    if file_path.endswith(PROTO_SUFFIX) and self.isApiFile(file_path):
      package_name, error_message = self.packageNameForProto(file_path)
      if package_name is None:
        error_messages += error_message
    return error_messages

  def checkSourcePath(self, file_path):
    error_messages = self.checkFileContents(file_path, self.checkSourceLine)

    if not file_path.endswith(DOCS_SUFFIX):
      if not file_path.endswith(PROTO_SUFFIX):
        error_messages += self.checkNamespace(file_path)
        command = ("%s --include_dir_order %s --path %s | diff %s -" %
                   (HEADER_ORDER_PATH, self.include_dir_order, file_path, file_path))
        error_messages += self.executeCommand(command, "header_order.py check failed", file_path)
      command = ("%s %s | diff %s -" % (CLANG_FORMAT_PATH, file_path, file_path))
      error_messages += self.executeCommand(command, "clang-format check failed", file_path)

    if file_path.endswith(PROTO_SUFFIX) and self.isApiFile(file_path):
      package_name, error_message = self.packageNameForProto(file_path)
      if package_name is None:
        error_messages += error_message
    return error_messages

  # Example target outputs are:
  #   - "26,27c26"
  #   - "12,13d13"
  #   - "7a8,9"
  def executeCommand(self,
                     command,
                     error_message,
                     file_path,
                     regex=re.compile(r"^(\d+)[a|c|d]?\d*(?:,\d+[a|c|d]?\d*)?$")):
    try:
      output = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT).strip()
      if output:
        return output.decode('utf-8').split("\n")
      return []
    except subprocess.CalledProcessError as e:
      if (e.returncode != 0 and e.returncode != 1):
        return ["ERROR: something went wrong while executing: %s" % e.cmd]
      # In case we can't find any line numbers, record an error message first.
      error_messages = ["%s for file: %s" % (error_message, file_path)]
      for line in e.output.decode('utf-8').splitlines():
        for num in regex.findall(line):
          error_messages.append("  %s:%s" % (file_path, num))
      return error_messages

  def fixHeaderOrder(self, file_path):
    command = "%s --rewrite --include_dir_order %s --path %s" % (HEADER_ORDER_PATH,
                                                                 self.include_dir_order, file_path)
    if os.system(command) != 0:
      return ["header_order.py rewrite error: %s" % (file_path)]
    return []

  def clangFormat(self, file_path):
    command = "%s -i %s" % (CLANG_FORMAT_PATH, file_path)
    if os.system(command) != 0:
      return ["clang-format rewrite error: %s" % (file_path)]
    return []

  def checkFormat(self, file_path):
    if file_path.startswith(EXCLUDED_PREFIXES):
      return []

    if not file_path.endswith(SUFFIXES):
      return []

    error_messages = []
    # Apply fixes first, if asked, and then run checks. If we wind up attempting to fix
    # an issue, but there's still an error, that's a problem.
    try_to_fix = self.operation_type == "fix"
    if self.isBuildFile(file_path) or self.isStarlarkFile(file_path) or self.isWorkspaceFile(
        file_path):
      if try_to_fix:
        error_messages += self.fixBuildPath(file_path)
      error_messages += self.checkBuildPath(file_path)
    else:
      if try_to_fix:
        error_messages += self.fixSourcePath(file_path)
      error_messages += self.checkSourcePath(file_path)

    if error_messages:
      return ["From %s" % file_path] + error_messages
    return error_messages

  def checkFormatReturnTraceOnError(self, file_path):
    """Run checkFormat and return the traceback of any exception."""
    try:
      return self.checkFormat(file_path)
    except:
      return traceback.format_exc().split("\n")

  def checkOwners(self, dir_name, owned_directories, error_messages):
    """Checks to make sure a given directory is present either in CODEOWNERS or OWNED_EXTENSIONS
    Args:
      dir_name: the directory being checked.
      owned_directories: directories currently listed in CODEOWNERS.
      error_messages: where to put an error message for new unowned directories.
    """
    found = False
    for owned in owned_directories:
      if owned.startswith(dir_name) or dir_name.startswith(owned):
        found = True
    if not found and dir_name not in UNOWNED_EXTENSIONS:
      error_messages.append("New directory %s appears to not have owners in CODEOWNERS" % dir_name)

  def checkApiShadowStarlarkFiles(self, file_path, error_messages):
    command = "diff -u "
    command += file_path + " "
    api_shadow_starlark_path = self.api_shadow_root + re.sub(r"\./api/", '', file_path)
    command += api_shadow_starlark_path

    error_message = self.executeCommand(command, "invalid .bzl in generated_api_shadow", file_path)
    if self.operation_type == "check":
      error_messages += error_message
    elif self.operation_type == "fix" and len(error_message) != 0:
      shutil.copy(file_path, api_shadow_starlark_path)

    return error_messages

  def checkFormatVisitor(self, arg, dir_name, names):
    """Run checkFormat in parallel for the given files.
    Args:
      arg: a tuple (pool, result_list, owned_directories, error_messages)
        pool and result_list are for starting tasks asynchronously.
        owned_directories tracks directories listed in the CODEOWNERS file.
        error_messages is a list of string format errors.
      dir_name: the parent directory of the given files.
      names: a list of file names.
    """

    # Unpack the multiprocessing.Pool process pool and list of results. Since
    # python lists are passed as references, this is used to collect the list of
    # async results (futures) from running checkFormat and passing them back to
    # the caller.
    pool, result_list, owned_directories, error_messages = arg

    # Sanity check CODEOWNERS.  This doesn't need to be done in a multi-threaded
    # manner as it is a small and limited list.
    source_prefix = './source/'
    full_prefix = './source/extensions/'
    # Check to see if this directory is a subdir under /source/extensions
    # Also ignore top level directories under /source/extensions since we don't
    # need owners for source/extensions/access_loggers etc, just the subdirectories.
    if dir_name.startswith(full_prefix) and '/' in dir_name[len(full_prefix):]:
      self.checkOwners(dir_name[len(source_prefix):], owned_directories, error_messages)

    for file_name in names:
      if dir_name.startswith("./api") and self.isStarlarkFile(file_name):
        result = pool.apply_async(self.checkApiShadowStarlarkFiles,
                                  args=(dir_name + "/" + file_name, error_messages))
        result_list.append(result)
      result = pool.apply_async(self.checkFormatReturnTraceOnError,
                                args=(dir_name + "/" + file_name,))
      result_list.append(result)

  # checkErrorMessages iterates over the list with error messages and prints
  # errors and returns a bool based on whether there were any errors.
  def checkErrorMessages(self, error_messages):
    if error_messages:
      for e in error_messages:
        print("ERROR: %s" % e)
      return True
    return False


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description="Check or fix file format.")
  parser.add_argument("operation_type",
                      type=str,
                      choices=["check", "fix"],
                      help="specify if the run should 'check' or 'fix' format.")
  parser.add_argument(
      "target_path",
      type=str,
      nargs="?",
      default=".",
      help="specify the root directory for the script to recurse over. Default '.'.")
  parser.add_argument("--add-excluded-prefixes",
                      type=str,
                      nargs="+",
                      help="exclude additional prefixes.")
  parser.add_argument("-j",
                      "--num-workers",
                      type=int,
                      default=multiprocessing.cpu_count(),
                      help="number of worker processes to use; defaults to one per core.")
  parser.add_argument("--api-prefix", type=str, default="./api/", help="path of the API tree.")
  parser.add_argument("--api-shadow-prefix",
                      type=str,
                      default="./generated_api_shadow/",
                      help="path of the shadow API tree.")
  parser.add_argument("--skip_envoy_build_rule_check",
                      action="store_true",
                      help="skip checking for '@envoy//' prefix in build rules.")
  parser.add_argument("--namespace_check",
                      type=str,
                      nargs="?",
                      default="Envoy",
                      help="specify namespace check string. Default 'Envoy'.")
  parser.add_argument("--namespace_check_excluded_paths",
                      type=str,
                      nargs="+",
                      default=[],
                      help="exclude paths from the namespace_check.")
  parser.add_argument("--build_fixer_check_excluded_paths",
                      type=str,
                      nargs="+",
                      default=[],
                      help="exclude paths from envoy_build_fixer check.")
  parser.add_argument("--bazel_tools_check_excluded_paths",
                      type=str,
                      nargs="+",
                      default=[],
                      help="exclude paths from bazel_tools check.")
  parser.add_argument("--include_dir_order",
                      type=str,
                      default=",".join(common.includeDirOrder()),
                      help="specify the header block include directory order.")
  args = parser.parse_args()
  if args.add_excluded_prefixes:
    EXCLUDED_PREFIXES += tuple(args.add_excluded_prefixes)
  format_checker = FormatChecker(args)

  # Check whether all needed external tools are available.
  ct_error_messages = format_checker.checkTools()
  if format_checker.checkErrorMessages(ct_error_messages):
    sys.exit(1)

  # Returns the list of directories with owners listed in CODEOWNERS. May append errors to
  # error_messages.
  def ownedDirectories(error_messages):
    owned = []
    maintainers = [
        '@mattklein123', '@htuch', '@alyssawilk', '@zuercher', '@lizan', '@snowp', '@asraa',
        '@yavlasov', '@junr03', '@dio', '@jmarantz', '@antoniovicente'
    ]

    try:
      with open('./CODEOWNERS') as f:
        for line in f:
          # If this line is of the form "extensions/... @owner1 @owner2" capture the directory
          # name and store it in the list of directories with documented owners.
          m = EXTENSIONS_CODEOWNERS_REGEX.search(line)
          if m is not None and not line.startswith('#'):
            owned.append(m.group(1).strip())
            owners = re.findall('@\S+', m.group(2).strip())
            if len(owners) < 2:
              error_messages.append("Extensions require at least 2 owners in CODEOWNERS:\n"
                                    "    {}".format(line))
            maintainer = len(set(owners).intersection(set(maintainers))) > 0
            if not maintainer:
              error_messages.append("Extensions require at least one maintainer OWNER:\n"
                                    "    {}".format(line))

      return owned
    except IOError:
      return []  # for the check format tests.

  # Calculate the list of owned directories once per run.
  error_messages = []
  owned_directories = ownedDirectories(error_messages)

  if os.path.isfile(args.target_path):
    error_messages += format_checker.checkFormat("./" + args.target_path)
  else:
    results = []

    def PooledCheckFormat(path_predicate):
      pool = multiprocessing.Pool(processes=args.num_workers)
      # For each file in target_path, start a new task in the pool and collect the
      # results (results is passed by reference, and is used as an output).
      for root, _, files in os.walk(args.target_path):
        format_checker.checkFormatVisitor((pool, results, owned_directories, error_messages), root,
                                          [f for f in files if path_predicate(f)])

      # Close the pool to new tasks, wait for all of the running tasks to finish,
      # then collect the error messages.
      pool.close()
      pool.join()

    # We first run formatting on non-BUILD files, since the BUILD file format
    # requires analysis of srcs/hdrs in the BUILD file, and we don't want these
    # to be rewritten by other multiprocessing pooled processes.
    PooledCheckFormat(lambda f: not format_checker.isBuildFile(f))
    PooledCheckFormat(lambda f: format_checker.isBuildFile(f))

    error_messages += sum((r.get() for r in results), [])

  if format_checker.checkErrorMessages(error_messages):
    print("ERROR: check format failed. run 'tools/code_format/check_format.py fix'")
    sys.exit(1)

  if args.operation_type == "check":
    print("PASS")
