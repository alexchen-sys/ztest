const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const root = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
        // std.c.getenv ($VIRTUAL_ENV, $PATH) needs libc explicitly linked.
        .link_libc = true,
    });

    const exe = b.addExecutable(.{
        .name = "ztest",
        .root_module = root,
    });
    b.installArtifact(exe);

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
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_tests.step);
}
