<?php
// List the simulated learner accounts: plan number -> Moodle user id (read-only).
//
// Usage: php export_simulated_users.php
//
// A plan's learner number N is the account with ID number SIM-NNNN (see
// inject_attempts.php), so annotate-plan can tie each planned answer to the
// Moodle user who gave it.

require(__DIR__ . '/lib.php');

$users = [];
foreach (setup_simulated_users() as $user) {
    $number = (int) substr($user->idnumber, strlen(SIM_IDNUMBER_PREFIX));
    $users[$number] = (int) $user->id;
}
setup_result(['users' => (object) $users]);
