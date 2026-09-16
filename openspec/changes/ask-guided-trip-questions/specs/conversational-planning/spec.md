## MODIFIED Requirements

### Requirement: Planning intent does not immediately start formal planning
The Chat Agent SHALL distinguish ordinary conversation from a possible formal-planning request and SHALL NOT create a formal PlanningRun solely because travel-planning intent was inferred.

#### Scenario: User asks a general travel question
- **WHEN** a user asks whether October is suitable for visiting Yunnan
- **THEN** the system responds as ordinary Chat without creating a formal planning run

#### Scenario: User expresses an incomplete planning intention
- **WHEN** a user asks for a Yunnan itinerary without the required date information
- **THEN** the system creates or updates a PlanningBrief and requests the missing required information

#### Scenario: User expresses planning intent without any extractable detail
- **WHEN** a user only expresses a wish to travel without naming a destination, dates or duration
- **THEN** the system still creates a PlanningBrief and begins asking for its fields one by one, instead of only replying that information is missing

### Requirement: Structured planning brief
The system SHALL maintain an editable PlanningBrief containing the structured requirements collected from conversation, its readiness status, and the source conversation.

`ready` SHALL mean that the required information is complete and the brief may be submitted. It SHALL NOT mean that field questioning has finished; whether every field has been answered or declined is a separate, presentation-only fact.

#### Scenario: Update a brief from a follow-up answer
- **WHEN** the user supplies dates requested for an existing collecting brief
- **THEN** the system updates that brief rather than creating a duplicate brief

#### Scenario: Optional preferences are absent
- **WHEN** destination and required date information are complete but optional budget or food preferences are absent
- **THEN** the brief can become ready using documented defaults

#### Scenario: Duration is present without calendar dates
- **WHEN** the user provides a destination and trip duration but no concrete start and end dates
- **THEN** the brief remains collecting and identifies the missing calendar dates required by formal planning

#### Scenario: Concrete date range completes the brief
- **WHEN** the brief contains a destination plus valid start and end dates
- **THEN** the brief becomes ready even when optional preferences are absent

#### Scenario: Ready while questioning continues
- **WHEN** a brief already contains a destination and valid dates but arrival time, departure time, budget or preferences have not been asked yet
- **THEN** the brief is `ready` and submittable, while the conversation keeps asking its remaining questions

### Requirement: Explicit formal-planning confirmation
The system SHALL present a user-visible summary of a ready PlanningBrief and SHALL require an explicit submit action before creating a formal PlanningRun. A conversational request to start planning SHALL NOT create a run directly; it SHALL end the remaining field questions and present the summary for confirmation.

#### Scenario: Confirm a ready brief
- **WHEN** the user selects “开始正式规划” on a ready brief
- **THEN** the system submits the brief and creates exactly one formal PlanningRun

#### Scenario: Continue editing a ready brief
- **WHEN** the user chooses to adjust a ready brief
- **THEN** the system keeps the brief editable and does not start formal planning

#### Scenario: User asks to start without answering the remaining questions
- **WHEN** a user says “别问了，直接开始规划” while optional fields have not been asked
- **THEN** the system marks the remaining optional fields as handled, presents the requirement summary, and does not create a PlanningRun until the user explicitly confirms
