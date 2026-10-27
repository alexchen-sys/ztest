const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // Build-time freshness gate: the monolithic embedded bundle is generated
    // from the reviewed source modules, never edited independently.
    const bundle_check = b.addSystemCommand(&.{ "python3", "scripts/bundle_shim.py", "--check" });

    // Release artifacts must be sterile: debug info embeds absolute build paths
    // (cache dirs, toolchain dirs, source roots) into the binary and inflates it
    // ~5x. Strip by default for every non-Debug build; `-Dstrip=false` opts out.
    // NB: the enum members are lowercase (`.debug`) in this toolchain; the
    // capitalized `Optimize.Debug` spelling is only a deprecated alias decl and
    // cannot be used as an inferred enum literal.
    const strip = b.option(bool, "strip", "Strip debug symbols") orelse (optimize != .debug);

    const root = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
        .strip = strip,
        // Linux stays libc-free: nothing there calls into libc (env is read via
        // std.process.Init.environ_map), so musl targets link fully static and
        // the glibc target keeps a stable, distro-independent ABI surface.
        // Darwin is different by design: the PAL reaches kqueue(2), libproc and
        // LOCAL_PEERCRED through libSystem, so macOS targets link libc and use
        // Zig's bundled Darwin headers/stubs (no Xcode SDK required).
        .link_libc = target.result.os.tag == .macos,
    });

    const exe = b.addExecutable(.{
        .name = "ztest",
        .root_module = root,
    });
    b.installArtifact(exe);
    exe.step.dependOn(&bundle_check.step);

    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    const run_step = b.step("run", "Run ztest");
    run_step.dependOn(&run_cmd.step);

    // Tests in src/spawn_arm.zig are reachable through main.zig's @import,
    // so this single test step runs the full native suite.
    // The compiled test binary is executed as a plain subprocess (exit-code
    // contract, results streamed line by line) instead of the build-runner
    // test-server protocol: that protocol's IPC can deadlock in sandboxed or
    // daemonized execution contexts, while direct execution is robust everywhere.
    const tests = b.addTest(.{ .root_module = root });
    const run_tests = std.Build.Step.Run.create(b, "run tests");
    run_tests.addArtifactArg(tests);
    run_tests.expectExitCode(0);
    // Do not hide the last started test behind subprocess capture. Native
    // process/PAL suites can hang independently of compilation, and the test
    // name is the minimum evidence needed to diagnose the platform backend.
    run_tests.stdio = .inherit;
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&bundle_check.step);
    test_step.dependOn(&run_tests.step);
}
