"""
Adapted from:
https://github.com/greenelab/BioBombe/blob/master/9.tcga-classify/scripts/tcga_util.py

"""

import os
import glob
import pandas as pd
from sklearn.metrics import (
    roc_auc_score,
    roc_curve,
    precision_recall_curve,
    average_precision_score,
)
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.linear_model import SGDClassifier
from dask_ml.model_selection import GridSearchCV

import config as cfg


def build_feature_dictionary(models_dir, load_data=False, store_train_test="both"):
    """
    Generate a nested dictionary of the directory structure pointing to compressed
    feature matrices for training and testing sets

    Arguments:
    models_dir - directory to look in for compressed features
    load_data - boolean if the data is to be loaded and stored in the
                return dict
    store_train_test - string indicating which data to load
                       ['train', 'test', 'both']

    Output: a nested dictionary storing the feature matrices from
            training and testing sets
    """

    z_matrix_dict = {}
    num_models = 0
    for signal in ["signal", "shuffled"]:
        z_matrix_dict[signal] = {}

        matrix_dir = os.path.join(
            models_dir, "ensemble_z_matrices"
        )

        for comp_dir in os.listdir(matrix_dir):
            matrix_comp_dir = os.path.join(matrix_dir, comp_dir)
            z_dim = comp_dir.split("_")[-1]
            z_matrix_dict[signal][z_dim] = {}

            pattern = ("{}/*shuffled_z_*" if signal == 'shuffled'
                                          else "{}/*_z_*")

            for z_file in glob.glob(pattern.format(matrix_comp_dir)):

                if signal == 'signal' and 'shuffled' in z_file:
                    continue

                seed = os.path.basename(z_file).split("_")[1]
                alg = os.path.basename(z_file).split("_")[0]

                if seed not in z_matrix_dict[signal][z_dim].keys():
                    z_matrix_dict[signal][z_dim][seed] = {}

                if alg not in z_matrix_dict[signal][z_dim][seed].keys():
                    z_matrix_dict[signal][z_dim][seed][alg] = {}

                if "_test_" in z_file:
                    if store_train_test == "train":
                        continue
                    if load_data:
                        z_matrix_dict[signal][z_dim][seed][alg]["test"] = pd.read_csv(
                            z_file, index_col=0, sep='\t'
                        )
                    else:
                        z_matrix_dict[signal][z_dim][seed][alg]["test"] = z_file
                else:
                    num_models += 1
                    if store_train_test == "test":
                        continue
                    if load_data:
                        z_matrix_dict[signal][z_dim][seed][alg]["train"] = pd.read_csv(
                            z_file, index_col=0, sep='\t'
                        )
                    else:
                        z_matrix_dict[signal][z_dim][seed][alg]["train"] = z_file

    return z_matrix_dict, num_models


def get_threshold_metrics(y_true, y_pred, drop=False):
    """
    Retrieve true/false positive rates and auroc/aupr for class predictions

    Arguments:
    y_true - an array of gold standard mutation status
    y_pred - an array of predicted mutation status
    drop - boolean if intermediate thresholds are dropped

    Output:
    dict of AUROC, AUPR, pandas dataframes of ROC and PR data, and cancer-type
    """
    roc_columns = ["fpr", "tpr", "threshold"]
    pr_columns = ["precision", "recall", "threshold"]

    roc_results = roc_curve(y_true, y_pred, drop_intermediate=drop)
    roc_items = zip(roc_columns, roc_results)
    roc_df = pd.DataFrame.from_dict(dict(roc_items))

    prec, rec, thresh = precision_recall_curve(y_true, y_pred)
    pr_df = pd.DataFrame.from_records([prec, rec]).T
    pr_df = pd.concat([pr_df, pd.Series(thresh)], ignore_index=True, axis=1)
    pr_df.columns = pr_columns

    auroc = roc_auc_score(y_true, y_pred, average="weighted")
    aupr = average_precision_score(y_true, y_pred, average="weighted")

    return {"auroc": auroc, "aupr": aupr, "roc_df": roc_df, "pr_df": pr_df}


def summarize_results(
    results, gene_or_cancertype, signal, z_dim, seed, algorithm, data_type
):
    """
    Given an input results file, summarize and output all pertinent files

    Arguments:
    results - a results object output from `get_threshold_metrics`
    gene_or_cancertype - the gene or cancertype of interest
    signal - the signal of interest
    z_dim - the internal bottleneck dimension of the compression model
    seed - the seed used to compress the data
    algorithm - the algorithm used to compress the data
    data_type - the type of data (either training, testing, or cv)
    """

    results_append_list = [
        gene_or_cancertype,
        signal,
        z_dim,
        seed,
        algorithm,
        data_type,
    ]

    metrics_out_ = [results["auroc"], results["aupr"]] + results_append_list

    roc_df_ = results["roc_df"]
    pr_df_ = results["pr_df"]

    roc_df_ = roc_df_.assign(
        predictor=gene_or_cancertype,
        signal=signal,
        z_dim=z_dim,
        seed=seed,
        algorithm=algorithm,
        data_type=data_type,
    )

    pr_df_ = pr_df_.assign(
        predictor=gene_or_cancertype,
        signal=signal,
        z_dim=z_dim,
        seed=seed,
        algorithm=algorithm,
        data_type=data_type,
    )

    return metrics_out_, roc_df_, pr_df_


def extract_coefficients(cv_pipeline, feature_names, signal, z_dim, seed, algorithm):
    """
    Pull out the coefficients from the trained classifiers

    Arguments:
    cv_pipeline - the trained sklearn cross validation pipeline
    feature_names - the column names of the x matrix used to train model (features)
    results - a results object output from `get_threshold_metrics`
    gene - the gene of interest
    signal - the signal of interest
    z_dim - the internal bottleneck dimension of the compression model
    seed - the seed used to compress the data
    algorithm - the algorithm used to compress the data
    """
    final_pipeline = cv_pipeline.best_estimator_
    final_classifier = final_pipeline.named_steps["classify"]

    coef_df = pd.DataFrame.from_dict(
        {"feature": feature_names, "weight": final_classifier.coef_[0]}
    )

    coef_df = (
        coef_df.assign(abs=coef_df["weight"].abs())
        .sort_values("abs", ascending=False)
        .reset_index(drop=True)
        .assign(signal=signal, z_dim=z_dim, seed=seed, algorithm=algorithm)
    )

    return coef_df


def process_y_matrix(
    y_mutation,
    y_copy,
    include_copy,
    gene,
    sample_freeze,
    mutation_burden,
    filter_count,
    filter_prop,
    output_directory,
    hyper_filter=5,
):
    """
    Combine copy number and mutation data and filter cancer-types to build y matrix

    Arguments:
    y_mutation - Pandas DataFrame of mutation status
    y_copy - Pandas DataFrame of copy number status
    include_copy - boolean if the copy number data should be included in status calc
    gene - string indicating gene of interest (used for writing proportion file)
    sample_feeze - pandas dataframe storing which samples to use
    mutation_burden - pandas dataframe storing log10 mutation counts
    filter_count - the number of positives or negatives required per cancer-type
    filter_prop - the proportion of positives or negatives required per cancer-type
    output_directory - the name of the directory to store the gene summary
    hyper_filter - the number of std dev above log10 mutation burden to filter

    Output:
    Write file of cancer-type filtering to disk and output a processed y vector
    """
    if include_copy:
        y_df = y_copy + y_mutation
    else:
        y_df = y_mutation

    y_df.loc[y_df > 1] = 1
    y_df = pd.DataFrame(y_df)
    y_df.columns = ["status"]

    y_df = (
        y_df.merge(
            sample_freeze, how="left", left_index=True, right_on="SAMPLE_BARCODE"
        )
        .set_index("SAMPLE_BARCODE")
        .merge(mutation_burden, left_index=True, right_index=True)
    )

    # Get statistics per gene and disease
    disease_counts_df = pd.DataFrame(y_df.groupby("DISEASE").sum()["status"])

    disease_proportion_df = disease_counts_df.divide(
        y_df["DISEASE"].value_counts(sort=False).sort_index(), axis=0
    )

    # Filter diseases with low counts or proportions for classification balance
    filter_disease_df = (disease_counts_df > filter_count) & (
        disease_proportion_df > filter_prop
    )
    filter_disease_df.columns = ["disease_included"]

    disease_stats_df = disease_counts_df.merge(
        disease_proportion_df,
        left_index=True,
        right_index=True,
        suffixes=("_count", "_proportion"),
    ).merge(filter_disease_df, left_index=True, right_index=True)

    filter_file = "{}_filtered_cancertypes.tsv".format(gene)
    filter_file = os.path.join(output_directory, filter_file)
    disease_stats_df.to_csv(filter_file, sep="\t")

    # Filter
    use_diseases = disease_stats_df.query("disease_included").index.tolist()
    burden_filter = y_df["log10_mut"] < hyper_filter * y_df["log10_mut"].std()
    y_df = y_df.loc[burden_filter, :].query("DISEASE in @use_diseases")

    return y_df


def process_y_matrix_cancertype(
    acronym, sample_freeze, mutation_burden, hyper_filter=5
):
    """
    Build a y vector based on cancer-type membership

    Arguments:
    acronym - the TCGA cancer-type barcode
    sample_freeze - a dataframe storing TCGA barcodes and cancer-types
    mutation_burden - a log10 mutation count per sample (added as covariate)

    Output:
    A y status DataFrame and a status count dataframe
    """
    y_df = sample_freeze.assign(status=0)
    y_df.loc[y_df.DISEASE == acronym, "status"] = 1

    y_df = y_df.set_index("SAMPLE_BARCODE").merge(
        mutation_burden, left_index=True, right_index=True
    )

    burden_filter = y_df["log10_mut"] < hyper_filter * y_df["log10_mut"].std()
    y_df = y_df.loc[burden_filter, :]

    count_df = pd.DataFrame(y_df.status.value_counts()).reset_index()
    count_df.columns = ["status", acronym]

    return y_df, count_df


def align_matrices(x_file_or_df, y, add_cancertype_covariate=True, algorithm=None):
    """
    Process the x matrix for the given input file and align x and y together

    Arguments:
    x_file_or_df - string location of the x matrix or matrix df itself
    y - pandas DataFrame storing status of corresponding samples
    algorithm - a string indicating which algorithm to subset the z matrices

    Output:
    The samples used to subset and the processed X and y matrices
    """
    # Load Data
    try:
        x_df = pd.read_csv(x_file_or_df, index_col=0, sep='\t')
    except:
        x_df = x_file_or_df

    # Subset samples
    use_samples = set(y.index).intersection(set(x_df.index))

    x_df = x_df.reindex(use_samples)
    y = y.reindex(use_samples)

    # Transform features to between zero and one
    x_scaled = StandardScaler().fit_transform(x_df)
    x_df = pd.DataFrame(x_scaled, columns=x_df.columns, index=x_df.index)

    # create covariate info
    mutation_covariate_df = pd.DataFrame(y.loc[:, "log10_mut"], index=y.index)

    # Merge log10 mutation burden covariate
    x_df = x_df.merge(mutation_covariate_df, left_index=True, right_index=True)

    if add_cancertype_covariate:
        # Merge features with covariate data
        covariate_df = pd.get_dummies(y.DISEASE)
        x_df = x_df.merge(covariate_df, left_index=True, right_index=True)

    return use_samples, x_df, y


def train_model(x_train, x_test, y_train, alphas, l1_ratios, n_folds=5, max_iter=1000):
    """
    Build the logic and sklearn pipelines to train x matrix based on input y

    Arguments:
    x_train - pandas DataFrame of feature matrix for training data
    x_test - pandas DataFrame of feature matrix for testing data
    y_train - pandas DataFrame of processed y matrix (output from align_matrices())
    alphas - list of alphas to perform cross validation over
    l1_ratios - list of l1 mixing parameters to perform cross validation over
    n_folds - int of how many folds of cross validation to perform
    max_iter - the maximum number of iterations to test until convergence

    Output:
    The full pipeline sklearn object and y matrix predictions for training, testing,
    and cross validation
    """
    # Setup the classifier parameters
    clf_parameters = {
        "classify__loss": ["log"],
        "classify__penalty": ["elasticnet"],
        "classify__alpha": alphas,
        "classify__l1_ratio": l1_ratios,
    }

    estimator = Pipeline(
        steps=[
            (
                "classify",
                SGDClassifier(
                    random_state=0,
                    class_weight="balanced",
                    loss="log",
                    max_iter=max_iter,
                    tol=1e-3,
                ),
            )
        ]
    )

    cv_pipeline = GridSearchCV(
        estimator=estimator,
        param_grid=clf_parameters,
        n_jobs=-1,
        cv=n_folds,
        scoring="roc_auc",
        return_train_score=True,
    )

    # Fit the model
    cv_pipeline.fit(X=x_train, y=y_train.status)

    # Obtain cross validation results
    y_cv = cross_val_predict(
        cv_pipeline.best_estimator_,
        X=x_train,
        y=y_train.status,
        cv=n_folds,
        method="decision_function",
    )

    # Get all performance results
    y_predict_train = cv_pipeline.decision_function(x_train)
    y_predict_test = cv_pipeline.decision_function(x_test)

    return cv_pipeline, y_predict_train, y_predict_test, y_cv


def check_status(file):
    """
    Check the status of a gene or cancer-type application

    Arguments:
    file - the file to check if it exists. If exists, then there is no need to rerun

    Output:
    boolean if the file exists or not
    """

    import os

    return os.path.isfile(file)


def build_good_feature_matrix(coef_df):
    """
    Compile training and testing feature matrices (X) given a coefficient DataFrame
    output from the various mutation classification analyses through repeated calls to
    `get_feature()`

    Arguments:
    coef_df - an input dataframe with the following information:
        z_dim, seed, and feature columns

    Output:
    Reads a series of compressed z matrices for training and testing and combines them
    into two dataframes. The dataframes represent the training and testing matrices
    used in downstream analyses.
    """
    all_test_features = []
    all_train_features = []
    for feature_idx, feature_row in coef_df.iterrows():
        z_dim = feature_row.z_dim
        seed = feature_row.seed
        feature = feature_row.feature

        test_feature_df, train_feature_df = get_feature(z_dim, seed, feature=feature)
        all_test_features.append(test_feature_df)
        all_train_features.append(train_feature_df)

    all_test_features_df = pd.concat(all_test_features, axis="columns")
    all_train_features_df = pd.concat(all_train_features, axis="columns")

    return all_test_features_df, all_train_features_df


def load_ensemble_dict(zs, seeds, algorithm=None, use_all_features=False):
    """
    Return algorithm dictionary storing ensemble matrices for single algorithms
    """
    algorithm_dict = {}
    ensemble_algorithm_dict = {}
    for signal in ["signal", "shuffled"]:
        all_ensemble_test = []
        all_ensemble_train = []
        if signal == "signal":
            shuffled = False
        else:
            shuffled = True
        algorithm_dict[signal] = {}
        ensemble_algorithm_dict[signal] = {}
        for z in zs:
            algorithm_dict[signal][str(z)] = {}

            ensemble_test = []
            ensemble_train = []
            for seed in seeds:

                # Inform status
                print(
                    "Now processing: {} for k = {} and seed = {}. Data is {}".format(
                        algorithm, z, seed, signal
                    )
                )

                # Load the specific z matrix (training and testing) and pull out feature
                test_feature_df, train_feature_df = get_feature(
                    z_dim=z, seed=seed, algorithm=algorithm, shuffled=shuffled
                )

                test_feature_df.columns = test_feature_df.columns + "_{}_{}_{}".format(
                    seed, z, signal
                )
                train_feature_df.columns = (
                    train_feature_df.columns + "_{}_{}_{}".format(seed, z, signal)
                )

                # Append to growing list
                if not use_all_features:
                    ensemble_test.append(test_feature_df)
                    ensemble_train.append(train_feature_df)
                else:
                    all_ensemble_test.append(test_feature_df)
                    all_ensemble_train.append(train_feature_df)

            # Load matrices into dictionary
            if not use_all_features:
                algorithm_dict[signal][str(z)]["test"] = pd.concat(
                    ensemble_test, axis="columns"
                )
                algorithm_dict[signal][str(z)]["train"] = pd.concat(
                    ensemble_train, axis="columns"
                )

        if use_all_features:
            ensemble_algorithm_dict[signal]["test"] = pd.concat(
                all_ensemble_test, axis="columns"
            )
            ensemble_algorithm_dict[signal]["train"] = pd.concat(
                all_ensemble_train, axis="columns"
            )

    if use_all_features:
        return ensemble_algorithm_dict
    else:
        return algorithm_dict
