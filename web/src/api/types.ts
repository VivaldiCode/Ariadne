import type { components } from "./schema";

type S = components["schemas"];

export type SearchHit = S["SearchHit"];
export type SearchResult = S["SearchResult"];
export type DocumentView = S["DocumentView"];
export type NeighborInfo = S["NeighborInfo"];
export type NeighborsResult = S["NeighborsResult"];
export type LinkView = S["LinkView"];
export type LinksPage = S["LinksPage"];
export type TranslationsResult = S["TranslationsResult"];
export type SourceInfo = S["SourceInfo"];
export type SourceCoverage = S["SourceCoverage"];
export type Overview = S["Overview"];
export type TopDocument = S["TopDocument"];
export type PrincipalUsage = S["PrincipalUsage"];
export type CurationItem = S["CurationItem"];
export type CurationDetail = S["CurationDetail"];
export type JobsOverview = S["JobsOverview"];
export type JobInfo = S["JobInfo"];
export type PrincipalView = S["PrincipalView"];
export type CreatedPrincipal = S["CreatedPrincipal"];
export type CollectionView = S["CollectionView"];
export type ConceptInfo = S["ConceptInfo"];
export type ConfigView = S["ConfigView"];
export type Health = S["Health"];
export type WhoAmI = S["WhoAmI"];
export type CourseView = S["CourseView"];
export type CourseDetail = S["CourseDetail"];
export type CourseVersion = S["VersionView"];
export type CourseLesson = S["LessonView"];
export type CourseExercise = S["ExerciseView"];
export type LessonInfo = S["LessonInfo"];
export type BookView = S["BookView"];
export type BookDetail = S["BookDetail"];
export type BookChapter = S["ChapterView"];
export type BookInfo = S["BookInfo"];
export type LicenseOverview = S["LicenseOverview"];
export type LicenseSource = S["LicenseSource"];
export type LicenseBlock = S["BlockView"];
export type LicenseDocumentsPage = S["LicenseDocumentsPage"];

export type CatalogItem = S["CatalogItem"];
export type CatalogView = S["CatalogView"];
export type CatalogFilters = S["CatalogFilters"];
export type DownloadView = S["DownloadView"];
export type BackupsView = S["BackupsView"];
export type BackupRun = S["RunView"];

export type ComputeOverview = S["ComputeOverview"];
export type ComputeControl = S["ComputeControlView"];
export type ComputeBackend = S["ComputeBackendView"];
export type ComputeKind = S["ComputeKindView"];
export type BatchRun = S["BatchRunView"];
export type BatchReviewResult = S["BatchReviewResult"];

export type IntegrationsOut = S["IntegrationsOut"];
export type IntegrationView = S["IntegrationView"];
export type IntegrationField = S["FieldView"];
export type VaultState = S["VaultState"];
export type IntegrationAudit = S["AuditOut"];
export type IntegrationTest = S["TestOut"];

export type LinkStatus = "proposed" | "accepted" | "rejected";
export type Decision = "published" | "pending_curator" | "needs_review" | "rejected";
