# symphony-devops-workflows
This repository is dedicated to Github workflows and actions

**Copyright (c) 2022 AVI-SPL Inc.
All Rights Reserved.**
 
**<font color="red">This repository is public so no sensitive data
such as logins or passwords should EVER be written in files, 
not even in files history!
Use GitHub secrets and provide them to workflows/actions via inputs
</font>**

This repository is dedicated to Github workflows and actions

## Assumptions
Workflows and actions in this repository assume that other actions
can be found at runtime under base path
`./.github/symphony-devops-workflows/`

Caller workflows must checkout this repository in above path

## Workflows
Workflows must be located in directory `.github\workflows`.
Otherwise workflow call will fail with error 
`invalid value workflow reference: references to workflows must be rooted in '.github/workflows'`

# Gotchas
## Calling an action with dynamic Git revision
It is not possible to use a variable in the "uses:" clause.
See issue `https://github.com/actions/runner/issues/1479`
Actions path syntax: (https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#jobsjob_idstepsuses)

For example this will cause a syntax error:
```
- name: Setup JDK
  uses: AVISPL/symphony-devops-workflows/actions/setup_jdk@${{ inputs.symphony-devops-workflows_ref }}
```

Solution is to checkout the actions repository at required Git revision 
(in example above, revision was `inputs.symphony-devops-workflows_ref`)
into a temporary directory and to call the actions with a local path.

For example, assuming the `symphony-devops-workflows` repository was checkouted 
into the `/.github/symphony-devops-workflows/` directory, `use` clause would look like:
```
- name: Setup JDK
  uses: ./.github/symphony-devops-workflows/actions/setup_jdk
```

## Calling an action with local path
Path must start with `./` otherwise GitHub will complain with a syntax error
