# Event-Driven Data Governance (Dataplex & BigQuery)

Ce repository contient une suite de Google Cloud Functions (Gen 2) écrites en Python. L'ensemble forme un moteur de Data Governance et de Column-Level Security 100% automatisé, basé sur **Google Cloud Dataplex (Knowledge Catalog)**, **Eventarc**, et les **Policy Tags BigQuery**.

## 🏛 Architecture Overview
Le système s'appuie sur les Cloud Audit Logs pour détecter en temps réel les changements de métadonnées dans Dataplex (Business Glossaries et Assets techniques). Eventarc route ces logs vers les Cloud Functions, qui propagent ensuite les métadonnées et appliquent physiquement les règles de sécurité sur les schémas BigQuery.

Cette architecture permet une approche **"Metadata-as-Code"**. Les Data Stewards travaillent uniquement sur l'UI Dataplex, et l'automatisation Python gère l'exécution technique sans intervention humaine.

## ⚙️ Les Workflows (Cloud Functions)

### 1. `metadata-sync` (Propagation Top-Down)
**Objectif :** Pousser les règles du Glossaire métier vers les Assets techniques.
Quand un Data Steward met à jour un Aspect (ex: une règle de masquage) sur un Terme du Glossaire, cette fonction trouve automatiquement toutes les tables/colonnes BigQuery linkées à ce terme et copie l'Aspect en downstream.

### 2. `term-sync` (Propagation Bottom-Up & Enforce)
**Objectif :** Appliquer les règles dès l'instant où un asset est linké au Glossaire.
Quand un Data Engineer relie une nouvelle table BigQuery à un Terme du Glossaire, ce script récupère les Aspects du terme parent, fait un pull sur l'asset BigQuery, et applique *immédiatement* les Policy Tags si des règles de masquage sont détectées.

### 3. `mask` (Security Enforcement)
**Objectif :** Lock dynamiquement les colonnes BigQuery.
Quand un Aspect de masquage (ex: `masking`, `encrypt`) est attaché à une colonne BigQuery dans Dataplex, cette fonction lit le mapping, récupère le Policy Tag BigQuery correspondant, et modifie le schéma de la table pour masquer la donnée selon les rôles IAM.

### 4. `unmask` (Security Removal)
**Objectif :** Retirer les tags de masquage en toute sécurité (Rollback).
Si un Data Steward supprime un Aspect de masquage sur une colonne dans Dataplex, ce script détecte l'update, check le schéma BigQuery, et retire le Policy Tag. **Note :** Le script ne retire que les tags gérés par l'automatisation (il ne touche pas aux policy tags ajoutés manuellement).

## 🚀 Setup & Déploiement
1. Activez les **Data Access Audit Logs** pour l'API `Dataplex API` dans Google Cloud IAM.
2. Donnez à votre service account Cloud Functions les rôles suivants :
   * `Dataplex Editor`
   * `Data Catalog Admin`
   * `BigQuery Admin`
3. Déployez les fonctions en utilisant des triggers Eventarc branchés sur :
   * **Service :** `dataplex.googleapis.com`
   * **Methods :** `google.cloud.dataplex.v1.CatalogService.UpdateEntry` et `google.cloud.dataplex.v1.CatalogService.CreateEntryLink`
